"""GET / (krewhub.kiro.internal) -- entrypoint que decide login vs lobby
sozinho. Ver app/main.py::root e README secao "GET / (raiz de
krewhub.kiro.internal)"."""

from __future__ import annotations

import urllib.parse


def test_root_without_cookie_redirects_to_login_no_store(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert r.headers["cache-control"] == "no-store"


def test_root_with_expired_cookie_redirects_to_login(client, expired_cookie):
    client.cookies.set("krewhub_session", expired_cookie("dev-a@test.local"))
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert r.headers["cache-control"] == "no-store"


def test_root_with_tampered_cookie_redirects_to_login(client, tampered_cookie):
    client.cookies.set("krewhub_session", tampered_cookie("dev-a@test.local"))
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_root_with_malformed_cookie_redirects_to_login_never_500(client):
    client.cookies.set("krewhub_session", "garbage-not-a-token")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_root_with_valid_session_redirects_to_own_lobby_from_token(client, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["cache-control"] == "no-store"
    expected = f"/devs/{urllib.parse.quote('dev-a@test.local', safe='')}/lobby"
    assert r.headers["location"] == expected


def test_root_never_trusts_query_param_owner_id(client, sign_cookie):
    """owner_id do redirect vem SEMPRE do cookie/token -- um query param
    tentando se passar por outro owner_id não pode influenciar o
    destino."""
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/?owner_id=dev-b@test.local", follow_redirects=False)
    assert "dev-a" in r.headers["location"]
    assert "dev-b" not in r.headers["location"]


def test_login_never_redirects_back_to_root(client, monkeypatch):
    """Evita loop /  -> /login -> / : /login sempre redireciona PRA
    FRENTE pro IdP (ou 501 se OIDC não configurado), nunca de volta pra
    raiz do KrewHub."""
    import app.main as main

    monkeypatch.setattr(
        main,
        "build_authorization_url",
        lambda settings: {
            "authorization_url": "https://idp.test.local/auth?foo=bar",
            "state": "st",
            "code_verifier": "cv",
        },
    )
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://idp.test.local/auth?foo=bar"
    assert r.headers["location"] != "/"


def test_callback_never_redirects_back_to_root(client, monkeypatch):
    """/callback, em sucesso, redireciona pro LOBBY do owner_id
    resolvido -- nunca pra raiz."""
    import app.main as main

    monkeypatch.setattr(
        main,
        "exchange_code",
        lambda settings, *, code, code_verifier: {"owner_id": "dev-a@test.local", "tokens": {}, "claims": {}},
    )
    main._pending_logins["state-123"] = "verifier-123"
    r = client.get("/callback?code=abc&state=state-123", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] != "/"
    assert r.headers["location"].startswith("/devs/")
