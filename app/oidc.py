"""Cliente OIDC genérico -- mesma implementação já provada em
`galaxy-far-far-away/clusters/family-cluster/kirohub/oidc-client-poc/oidc_client.py`
contra 3 issuers reais (Google, Microsoft, GitLab), adaptada aqui pra ser
importada por `main.py` em vez de rodada como script solto.

Config 100% via `Settings` (app.config) -- issuer/client_id/client_secret/
redirect_uri/scopes. Discovery document padrão resolve os endpoints; nenhum
provider é hardcoded."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from app.config import Settings

REQUIRED_DISCOVERY_FIELDS = ("authorization_endpoint", "token_endpoint", "jwks_uri")


class OIDCConfigError(RuntimeError):
    """Config ausente/inválida -- nunca um fallback silencioso."""


def discover(issuer: str) -> dict:
    if not issuer:
        raise OIDCConfigError(
            "KREWHUB_OIDC_ISSUER não configurada -- /login não pode construir "
            "a authorization URL sem saber qual IdP usar (config-driven: sem "
            "provider default)."
        )
    discovery_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    req = urllib.request.Request(discovery_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise OIDCConfigError(f"discovery falhou em {discovery_url}: {exc}") from exc

    missing = [f for f in REQUIRED_DISCOVERY_FIELDS if not doc.get(f)]
    if missing:
        raise OIDCConfigError(
            f"discovery document em {discovery_url} sem campos obrigatórios: {missing}"
        )
    return doc


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorization_url(settings: Settings) -> dict:
    """Monta a authorization URL (PKCE S256) a partir só da Settings.

    Levanta OIDCConfigError se issuer/client_id/redirect_uri não estiverem
    configurados -- é o comportamento correto pra essa fatia (config
    placeholder vazia = erro explícito, não um provider adivinhado)."""
    if not settings.oidc_client_id:
        raise OIDCConfigError("KREWHUB_OIDC_CLIENT_ID não configurada")
    if not settings.oidc_redirect_uri:
        raise OIDCConfigError("KREWHUB_OIDC_REDIRECT_URI não configurada")

    doc = discover(settings.oidc_issuer)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": settings.oidc_scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = doc["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
    return {"authorization_url": url, "state": state, "code_verifier": verifier}


def exchange_code(settings: Settings, *, code: str, code_verifier: str) -> dict:
    """Troca code por token + resolve claims. Não testado ao vivo ainda
    (decisão do Lucas: fica pra depois) -- implementado pra já existir
    quando a decisão vier, não é caminho morto."""
    doc = discover(settings.oidc_issuer)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oidc_redirect_uri,
        "client_id": settings.oidc_client_id,
        "code_verifier": code_verifier,
    }
    if settings.oidc_client_secret:
        data["client_secret"] = settings.oidc_client_secret
    body = urllib.parse.urlencode(data).encode("ascii")
    req = urllib.request.Request(
        doc["token_endpoint"],
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        tokens = json.loads(resp.read().decode("utf-8"))

    claims: dict = {}
    if "id_token" in tokens:
        payload_b64 = tokens["id_token"].split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    elif "access_token" in tokens and doc.get("userinfo_endpoint"):
        req = urllib.request.Request(
            doc["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            claims = json.loads(resp.read().decode("utf-8"))

    owner_id = claims.get("email") or claims.get("sub") or ""
    return {"tokens": tokens, "claims": claims, "owner_id": owner_id}
