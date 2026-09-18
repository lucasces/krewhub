"""Autenticacao dos proprios endpoints -- fecha o gap documentado em
"Fora de escopo desta fatia" (qualquer requisicao que chegasse ate o CHP
podia reconciliar/reemitir sessao pra qualquer owner_id, sem nenhuma
verificacao).

Token interno assinado (HMAC-SHA256), NAO o access_token do Keycloak.
Decisao documentada aqui e no README: validar o access_token do IdP
direto via JWKS (resolvido pelo discovery ja implementado em app/oidc.py)
tambem seria uma opcao valida -- mas um token interno proprio evita (a)
depender da rede ate o IdP em toda request protegida (nao so no
login/callback -- e ja tivemos erro de rede batendo no proxy do CHP
nesta mesma linha de trabalho, entao essa dependencia tem custo real
aqui) e (b) espalhar o access_token real do Keycloak -- que carrega
escopos/permissoes do IdP, nao so identidade -- por mais lugares (cookie
de browser) do que o necessario; o KrewHub so precisa saber "quem e" o
dev, nao o que o Keycloak deixaria esse token especifico fazer.

Mesma regra ja seguida pro token do kirocrew (README, "sessao e
credencial, nao persistida"): assinado com HMAC usando um segredo
proprio do servico (KREWHUB_SESSION_SECRET, um Secret aplicado direto
via kubectl, igual ao client-secret OIDC -- nunca versionado em git),
carrega so owner_id + expiracao, nao precisa de tabela/estado em
servidor pra ser revogado individualmente (mesmo trade-off ja aceito
alhures neste projeto).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class AuthTokenError(RuntimeError):
    """Token ausente/invalido/expirado -- nunca aceito silenciosamente."""


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def sign_session(owner_id: str, *, secret: str, ttl_seconds: int) -> str:
    """Emite um token `<payload_b64>.<assinatura_b64>` -- payload =
    {"owner_id", "exp"} (epoch, segundos). Levanta AuthTokenError (nao
    finge sucesso) se KREWHUB_SESSION_SECRET nao estiver configurada."""
    if not secret:
        raise AuthTokenError(
            "KREWHUB_SESSION_SECRET nao configurada -- nao da pra assinar sessao"
        )
    payload = json.dumps(
        {"owner_id": owner_id, "exp": int(time.time()) + ttl_seconds},
        separators=(",", ":"),
    ).encode("utf-8")
    payload_b64 = _b64e(payload)
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64e(sig)}"


def verify_session(token: str, *, secret: str) -> str:
    """Valida assinatura + expiracao, devolve owner_id. Levanta
    AuthTokenError pra QUALQUER problema (malformado, assinatura errada,
    expirado, secret ausente) -- caller decide o status HTTP (401/403),
    esta funcao nunca devolve um owner_id nao confiavel."""
    if not secret:
        raise AuthTokenError(
            "KREWHUB_SESSION_SECRET nao configurada -- nao da pra validar sessao"
        )
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError as exc:
        raise AuthTokenError("token malformado (esperava '<payload>.<assinatura>')") from exc

    expected_sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        given_sig = _b64d(sig_b64)
    except Exception as exc:
        raise AuthTokenError("assinatura malformada") from exc
    if not hmac.compare_digest(expected_sig, given_sig):
        raise AuthTokenError("assinatura invalida")

    try:
        payload = json.loads(_b64d(payload_b64))
    except Exception as exc:
        raise AuthTokenError("payload malformado") from exc

    owner_id = payload.get("owner_id")
    exp = payload.get("exp")
    if not owner_id or not isinstance(exp, int):
        raise AuthTokenError("payload sem 'owner_id'/'exp'")
    if time.time() > exp:
        raise AuthTokenError("token expirado")
    return owner_id
