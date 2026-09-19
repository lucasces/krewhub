"""app/oidc.py -- discovery + authorization URL (PKCE) + exchange de
code por token. SEM chamada de rede real: `urllib.request.urlopen` e'
mockado (app.oidc importa `urllib.request` inteiro e chama
`urllib.request.urlopen`, entao patcheamos esse atributo direto no
modulo oidc)."""

from __future__ import annotations

import base64
import json
import urllib.parse
from contextlib import contextmanager
from unittest import mock

import pytest

from app import oidc
from app.config import Settings

ISSUER = "https://idp.test.local/realms/test"

DISCOVERY_DOC = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
    "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
    "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
}


def _fake_response(payload: dict):
    """Fabrica um objeto usavel como `with urlopen(...) as resp: resp.read()`."""

    @contextmanager
    def _cm(*_args, **_kwargs):
        resp = mock.Mock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        yield resp

    return _cm


def _settings(**overrides) -> Settings:
    base = dict(
        k8s_kubeconfig="",
        k8s_context="test",
        dev_namespace="krewhub-devs",
        base_domain="kiro.internal",
        public_port="8080",
        kirocrew_image="img",
        storage_class="sc",
        storage_size="1Gi",
        chp_namespace="kirohub",
        chp_pod_label="app=x",
        chp_admin_port=8001,
        db_path=":memory:",
        session_ttl="24h",
        oidc_issuer=ISSUER,
        oidc_client_id="krewhub-test-client",
        oidc_client_secret="",
        oidc_redirect_uri="http://krewhub.kiro.internal:8080/callback",
        oidc_scopes="openid email profile",
        session_secret="s",
        auth_token_ttl_seconds=3600,
        kiro_identity_provider="",
        kiro_region="",
        self_host="",
        self_port=8080,
    )
    base.update(overrides)
    return Settings(**base)


def test_discover_resolves_all_endpoints_from_issuer(monkeypatch):
    monkeypatch.setattr(oidc.urllib.request, "urlopen", _fake_response(DISCOVERY_DOC))
    doc = oidc.discover(ISSUER)
    assert doc["authorization_endpoint"] == DISCOVERY_DOC["authorization_endpoint"]
    assert doc["token_endpoint"] == DISCOVERY_DOC["token_endpoint"]
    assert doc["jwks_uri"] == DISCOVERY_DOC["jwks_uri"]
    assert doc["end_session_endpoint"] == DISCOVERY_DOC["end_session_endpoint"]


def test_discover_without_issuer_raises():
    with pytest.raises(oidc.OIDCConfigError, match="ISSUER"):
        oidc.discover("")


def test_discover_missing_required_field_raises(monkeypatch):
    incomplete = {k: v for k, v in DISCOVERY_DOC.items() if k != "jwks_uri"}
    monkeypatch.setattr(oidc.urllib.request, "urlopen", _fake_response(incomplete))
    with pytest.raises(oidc.OIDCConfigError, match="jwks_uri"):
        oidc.discover(ISSUER)


def test_build_authorization_url_has_pkce_s256_and_correct_params(monkeypatch):
    monkeypatch.setattr(oidc.urllib.request, "urlopen", _fake_response(DISCOVERY_DOC))
    settings = _settings()
    result = oidc.build_authorization_url(settings)

    parsed = urllib.parse.urlsplit(result["authorization_url"])
    qs = urllib.parse.parse_qs(parsed.query)

    assert result["authorization_url"].startswith(DISCOVERY_DOC["authorization_endpoint"])
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == [settings.oidc_client_id]
    assert qs["redirect_uri"] == [settings.oidc_redirect_uri]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["state"] == [result["state"]]
    assert len(result["state"]) > 10
    assert len(result["code_verifier"]) > 10

    # code_challenge = base64url(sha256(code_verifier)) sem padding --
    # confirma que o challenge devolvido bate com o verifier devolvido
    # (não são valores desencontrados).
    import hashlib

    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(result["code_verifier"].encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert qs["code_challenge"] == [expected_challenge]


def test_build_authorization_url_without_client_id_raises(monkeypatch):
    monkeypatch.setattr(oidc.urllib.request, "urlopen", _fake_response(DISCOVERY_DOC))
    settings = _settings(oidc_client_id="")
    with pytest.raises(oidc.OIDCConfigError, match="CLIENT_ID"):
        oidc.build_authorization_url(settings)


def test_build_authorization_url_without_redirect_uri_raises(monkeypatch):
    monkeypatch.setattr(oidc.urllib.request, "urlopen", _fake_response(DISCOVERY_DOC))
    settings = _settings(oidc_redirect_uri="")
    with pytest.raises(oidc.OIDCConfigError, match="REDIRECT_URI"):
        oidc.build_authorization_url(settings)


def _id_token_for(claims: dict) -> str:
    """JWT-shaped string o suficiente pro parsing de `exchange_code`
    (header.payload.signature) -- exchange_code só decodifica o payload,
    nunca verifica a assinatura do id_token (isso é responsabilidade do
    /callback via nosso próprio cookie assinado, não deste módulo)."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _ordered_urlopen(*payloads: dict):
    """`discover()` é chamado ANTES do POST no token_endpoint dentro de
    `exchange_code` -- a 1a chamada de urlopen é sempre discovery, a 2a
    em diante segue `payloads` na ordem dada."""
    call_order = {"n": 0}

    def _urlopen(req, *_a, **_kw):
        idx = call_order["n"]
        call_order["n"] += 1
        resp = mock.Mock()
        resp.read.return_value = json.dumps(payloads[idx]).encode("utf-8")
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        return resp

    return _urlopen


def test_exchange_code_extracts_owner_id_from_email_claim(monkeypatch):
    token_response = {
        "access_token": "at-123",
        "id_token": _id_token_for({"email": "dev-a@test.local", "sub": "uuid-1"}),
    }
    monkeypatch.setattr(
        oidc.urllib.request, "urlopen", _ordered_urlopen(DISCOVERY_DOC, token_response)
    )
    settings = _settings()
    result = oidc.exchange_code(settings, code="the-code", code_verifier="the-verifier")
    assert result["owner_id"] == "dev-a@test.local"
    assert result["claims"]["sub"] == "uuid-1"
    assert result["tokens"]["access_token"] == "at-123"


def test_exchange_code_falls_back_to_sub_when_no_email(monkeypatch):
    token_response = {"id_token": _id_token_for({"sub": "uuid-only"})}
    monkeypatch.setattr(
        oidc.urllib.request, "urlopen", _ordered_urlopen(DISCOVERY_DOC, token_response)
    )
    settings = _settings()
    result = oidc.exchange_code(settings, code="c", code_verifier="v")
    assert result["owner_id"] == "uuid-only"
