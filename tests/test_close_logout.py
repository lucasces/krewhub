"""GET /close vs GET /logout -- ver README, seção "GET /close vs GET
/logout". `session_client.revoke_session` mockado (nenhum kubectl exec
real)."""

from __future__ import annotations

from unittest import mock

import pytest

from app import k8s_manager, session_client, store


@pytest.fixture
def provisioned(settings):
    with store.connect(settings.db_path) as conn:
        store.upsert(
            conn,
            owner_id="dev-a@test.local",
            slug="dev-a-test-local",
            namespace="krewhub-devs",
            host="dev-a-test-local.kiro.internal",
            status="routed",
        )


@pytest.fixture
def mocked_revoke(monkeypatch):
    calls = []

    def _fake(_c, *, namespace, slug):
        calls.append({"namespace": namespace, "slug": slug})
        return "✅ All dashboard sessions revoked."

    monkeypatch.setattr(session_client, "revoke_session", _fake)
    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    return calls


# ---------------------------------------------------------------------------
# GET /close
# ---------------------------------------------------------------------------


def test_close_requires_session(client, provisioned, mocked_revoke):
    r = client.get("/close")
    assert r.status_code == 401
    assert mocked_revoke == []


def test_close_revokes_kirocrew_session_and_does_not_touch_krewhub_cookie(
    client, provisioned, sign_cookie, mocked_revoke
):
    cookie = sign_cookie("dev-a@test.local")
    client.cookies.set("krewhub_session", cookie)
    r = client.get("/close", follow_redirects=False)

    assert r.status_code == 200
    assert "encerrada" in r.text.lower()
    assert "set-cookie" not in {h.lower() for h in r.headers.keys()}
    assert len(mocked_revoke) == 1
    assert mocked_revoke[0] == {"namespace": "krewhub-devs", "slug": "dev-a-test-local"}
    # O cookie do KrewHub continua o mesmo, servido pelo TestClient na
    # próxima chamada -- "continua logado" de verdade.
    assert client.cookies.get("krewhub_session") == cookie


def test_close_owner_id_comes_from_cookie_not_query(client, provisioned, sign_cookie, mocked_revoke):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    client.get("/close?owner_id=dev-b@test.local")
    assert mocked_revoke[0]["slug"] == "dev-a-test-local"


def test_close_404_when_owner_never_provisioned(client, sign_cookie, mocked_revoke):
    client.cookies.set("krewhub_session", sign_cookie("dev-nunca-provisionado@test.local"))
    r = client.get("/close")
    assert r.status_code == 404


def test_close_502_when_revocation_fails(client, provisioned, sign_cookie, monkeypatch):
    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    monkeypatch.setattr(
        session_client,
        "revoke_session",
        mock.Mock(side_effect=session_client.SessionError("kirocrew logout não confirmou sucesso")),
    )
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/close")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# GET /logout
# ---------------------------------------------------------------------------


def test_logout_revokes_kirocrew_and_clears_krewhub_cookie_and_redirects(
    client, provisioned, sign_cookie, mocked_revoke
):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/logout", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    set_cookie = r.headers["set-cookie"]
    assert "krewhub_session=" in set_cookie
    assert "Max-Age=0" in set_cookie
    assert len(mocked_revoke) == 1
    assert mocked_revoke[0] == {"namespace": "krewhub-devs", "slug": "dev-a-test-local"}


def test_logout_without_cookie_is_idempotent_no_revoke_attempted(client, mocked_revoke):
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert "Max-Age=0" in r.headers["set-cookie"]
    assert mocked_revoke == []


def test_logout_with_malformed_cookie_never_errors(client, mocked_revoke):
    client.cookies.set("krewhub_session", "garbage-not-a-token")
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert mocked_revoke == []


def test_logout_best_effort_when_revocation_raises_still_clears_cookie_and_redirects(
    client, provisioned, sign_cookie, monkeypatch
):
    """A revogação do kirocrew é melhor esforço dentro de /logout -- uma
    falha aqui NUNCA pode travar o logout do KrewHub em si."""
    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    monkeypatch.setattr(
        session_client,
        "revoke_session",
        mock.Mock(side_effect=session_client.SessionError("gateway indisponível")),
    )
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/logout", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert "Max-Age=0" in r.headers["set-cookie"]


def test_logout_best_effort_when_owner_never_provisioned_still_logs_out(
    client, sign_cookie, mocked_revoke
):
    """owner_id válido no cookie, mas nunca provisionado (sem
    namespace/slug pra revogar) -- ainda assim o /logout do KrewHub
    completa normalmente."""
    client.cookies.set("krewhub_session", sign_cookie("dev-nunca-provisionado@test.local"))
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert "Max-Age=0" in r.headers["set-cookie"]
    assert mocked_revoke == []
