"""app/auth.py -- assinatura/verificacao do token de sessao propria do
KrewHub (HMAC-SHA256, NAO o access_token do Keycloak). Testes puros, sem
FastAPI/k8s -- so as duas funcoes."""

from __future__ import annotations

import pytest

from app import auth

SECRET = "unit-test-secret"


def test_valid_token_roundtrips_owner_id():
    token = auth.sign_session("dev-a@test.local", secret=SECRET, ttl_seconds=60)
    assert auth.verify_session(token, secret=SECRET) == "dev-a@test.local"


def test_expired_token_fails():
    token = auth.sign_session("dev-a@test.local", secret=SECRET, ttl_seconds=-1)
    with pytest.raises(auth.AuthTokenError, match="expirado"):
        auth.verify_session(token, secret=SECRET)


def test_malformed_token_fails():
    with pytest.raises(auth.AuthTokenError, match="malformado"):
        auth.verify_session("nao-tem-ponto-nenhum", secret=SECRET)


def test_malformed_signature_b64_fails():
    payload_b64 = auth.sign_session("dev-a@test.local", secret=SECRET, ttl_seconds=60).split(".")[0]
    with pytest.raises(auth.AuthTokenError):
        auth.verify_session(f"{payload_b64}.not-valid-base64!!!", secret=SECRET)


def test_tampered_signature_fails():
    token = auth.sign_session("dev-a@test.local", secret=SECRET, ttl_seconds=60)
    payload_b64, sig_b64 = token.rsplit(".", 1)
    # Primeiro char, não o último -- ver conftest.py::tampered_cookie
    # pro motivo (último char de um digest de 32 bytes em base64url tem
    # bits de padding que alguns pares de caracteres não corrompem).
    alt = "A" if sig_b64[0] != "A" else "B"
    tampered = f"{payload_b64}.{alt}{sig_b64[1:]}"
    with pytest.raises(auth.AuthTokenError, match="assinatura inv"):
        auth.verify_session(tampered, secret=SECRET)


def test_wrong_secret_fails():
    """Mesma assinatura, secret diferente na verificacao -- simula um
    segundo processo/deploy com KREWHUB_SESSION_SECRET desalinhado."""
    token = auth.sign_session("dev-a@test.local", secret=SECRET, ttl_seconds=60)
    with pytest.raises(auth.AuthTokenError, match="assinatura inv"):
        auth.verify_session(token, secret="outro-secret-completamente-diferente")


def test_tampered_payload_owner_id_fails():
    """Trocar o owner_id no payload (sem re-assinar) precisa falhar --
    e' exatamente o ataque que a assinatura HMAC previne."""
    import base64
    import json

    token = auth.sign_session("dev-a@test.local", secret=SECRET, ttl_seconds=60)
    payload_b64, sig_b64 = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    payload["owner_id"] = "dev-b@test.local"
    new_payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    forged = f"{new_payload_b64}.{sig_b64}"
    with pytest.raises(auth.AuthTokenError, match="assinatura inv"):
        auth.verify_session(forged, secret=SECRET)


def test_sign_session_requires_secret():
    with pytest.raises(auth.AuthTokenError):
        auth.sign_session("dev-a@test.local", secret="", ttl_seconds=60)


def test_verify_session_requires_secret():
    with pytest.raises(auth.AuthTokenError):
        auth.verify_session("whatever.whatever", secret="")
