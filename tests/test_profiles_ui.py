"""Testes da página de gerência de profiles (multi-profile UI) --
`GET /devs/{owner_id}/profiles/ui`, `GET /devs/{owner_id}/profiles/new`
e `POST /devs/{owner_id}/profiles/close`.

Suite RÁPIDA e OFFLINE como o resto (ver tests/conftest.py): SQLite de
verdade num tmp_path por teste; o teardown k8s do `/profiles/close` é
mockado (`_close_dev_session` -> `k8s_manager.teardown_dev_workload` +
`session_client.revoke_session`), nunca toca cluster real. `owner_id`
URL-encoded (`@` -> `%40`), igual aos outros testes de rota de dev.
"""

from __future__ import annotations

import urllib.parse
from unittest import mock

import pytest

from app import k8s_manager, session_client, store
from app.extensions import runtime as ext_runtime

OWNER = "dev-a@test.local"


def _path(owner_id: str) -> str:
    return urllib.parse.quote(owner_id, safe="@")


@pytest.fixture
def provisioned_two(settings):
    """Dois profiles provisionados do mesmo owner (default + work) +
    um terceiro owner, pra provar o escopo por owner."""
    with store.connect(settings.db_path) as conn:
        store.upsert(conn, owner_id=OWNER, profile="default", slug="dev-a-test-local",
                     namespace="krewhub-devs", host="dev-a-test-local.kiro.internal", status="routed")
        store.upsert(conn, owner_id=OWNER, profile="work", slug="dev-a-test-local-work",
                     namespace="krewhub-devs", host="dev-a-test-local-work.kiro.internal", status="ready")
        store.upsert(conn, owner_id="other@test.local", profile="default", slug="other",
                     namespace="krewhub-devs", host="other.kiro.internal", status="routed")


@pytest.fixture
def mocked_teardown(monkeypatch):
    """`_close_dev_session` encadeia revoke_session + teardown_dev_workload;
    ambos mockados -- estes testes cobrem só a rota (auth/CSRF/escopo de
    profile/erro), não o teardown de baixo nível."""
    calls = []

    def _fake_teardown(_c, *, namespace, slug):
        calls.append({"namespace": namespace, "slug": slug})
        return {"namespace": namespace, "slug": slug, "steps": {"pod": "deleted"}}

    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    monkeypatch.setattr(k8s_manager, "teardown_dev_workload", _fake_teardown)
    monkeypatch.setattr(session_client, "revoke_session", lambda *a, **kw: "ok")
    return calls


def _close_csrf(settings, owner_id, profile):
    return ext_runtime.make_csrf(
        settings.session_secret, owner_id, "_profiles", f"close:{profile}"
    )


# ---------------------------------------------------------------------------
# GET /profiles/ui -- render + auth + active marker
# ---------------------------------------------------------------------------


def test_profiles_ui_requires_session(client, provisioned_two):
    """Sem credencial, chamada programática (sem Accept text/html) -> 401."""
    r = client.get(f"/devs/{_path(OWNER)}/profiles/ui")
    assert r.status_code == 401


def test_profiles_ui_lists_the_owners_profiles(client, provisioned_two, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie(OWNER))
    r = client.get(f"/devs/{_path(OWNER)}/profiles/ui")
    assert r.status_code == 200
    body = r.text
    assert "default" in body
    assert "work" in body
    assert "dev-a-test-local-work" in body
    assert "dev-a-test-local-work.kiro.internal" in body
    assert "?profile=work" in body
    assert f"/devs/{_path(OWNER)}/lobby" in body
    assert "other.kiro.internal" not in body


def test_profiles_ui_marks_active_profile_from_token(client, provisioned_two, sign_cookie):
    """O marcador "(sessão atual)" vem do PRÓPRIO token -- com um cookie
    de profile=work, só a linha work é marcada."""
    client.cookies.set("krewhub_session", sign_cookie(OWNER, profile="work"))
    body = client.get(f"/devs/{_path(OWNER)}/profiles/ui").text
    assert "(sessão atual)" in body
    assert body.count("(sessão atual)") == 1


def test_profiles_ui_only_owner_sees_own(client, provisioned_two, sign_cookie):
    """Credencial de outro owner na URL deste owner -> 403 (require_owner)."""
    client.cookies.set("krewhub_session", sign_cookie("other@test.local"))
    r = client.get(f"/devs/{_path(OWNER)}/profiles/ui")
    assert r.status_code == 403


def test_profiles_ui_shows_create_form(client, provisioned_two, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie(OWNER))
    body = client.get(f"/devs/{_path(OWNER)}/profiles/ui").text
    assert f'action="/devs/{_path(OWNER)}/profiles/new"' in body
    assert 'name="profile"' in body


def test_single_profile_dev_view_unchanged(client, settings, sign_cookie):
    """Um dev com só o profile default: a página renderiza a única
    sessão, sem link de profile não-default -- a experiência de sessão
    única fica intacta."""
    with store.connect(settings.db_path) as conn:
        store.upsert(conn, owner_id=OWNER, profile="default", slug="dev-a-test-local",
                     namespace="krewhub-devs", host="dev-a-test-local.kiro.internal", status="routed")
    client.cookies.set("krewhub_session", sign_cookie(OWNER))
    body = client.get(f"/devs/{_path(OWNER)}/profiles/ui").text
    assert "default" in body
    assert "?profile=" not in body


# ---------------------------------------------------------------------------
# GET /profiles/new -- criar/entrar
# ---------------------------------------------------------------------------


def test_create_profile_routes_to_lobby(client, settings, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie(OWNER))
    r = client.get(f"/devs/{_path(OWNER)}/profiles/new?profile=home", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/devs/{_path(OWNER)}/lobby?profile=home"


def test_create_profile_default_routes_to_bare_lobby(client, settings, sign_cookie):
    """Nome 'default' é válido e leva ao lobby SEM query (backward-compat)."""
    client.cookies.set("krewhub_session", sign_cookie(OWNER))
    r = client.get(f"/devs/{_path(OWNER)}/profiles/new?profile=default", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/devs/{_path(OWNER)}/lobby"


def test_create_profile_normalizes_case(client, settings, sign_cookie):
    """`_resolve_profile` normaliza (lower) -- 'Work' vira 'work'."""
    client.cookies.set("krewhub_session", sign_cookie(OWNER))
    r = client.get(f"/devs/{_path(OWNER)}/profiles/new?profile=Work", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/devs/{_path(OWNER)}/lobby?profile=work"


def test_create_profile_rejects_invalid_name(client, settings, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie(OWNER))
    r = client.get(f"/devs/{_path(OWNER)}/profiles/new?profile=bad__name!", follow_redirects=False)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# POST /profiles/close -- fechar UM profile, escopado + CSRF
# ---------------------------------------------------------------------------


def test_close_profile_is_profile_scoped(client, settings, provisioned_two, sign_cookie, mocked_teardown):
    """Fechar 'work' derruba o slug de 'work', não o default; o cookie é
    do default (prova que o alvo vem do FORM, não do token)."""
    client.cookies.set("krewhub_session", sign_cookie(OWNER, profile="default"))
    r = client.post(
        f"/devs/{_path(OWNER)}/profiles/close",
        data={"profile": "work", "csrf": _close_csrf(settings, OWNER, "work")},
    )
    assert r.status_code == 200
    assert mocked_teardown == [{"namespace": "krewhub-devs", "slug": "dev-a-test-local-work"}]
    with store.connect(settings.db_path) as conn:
        assert store.get(conn, OWNER, "work")["status"] == "closed"
        assert store.get(conn, OWNER, "default")["status"] == "routed"


def test_close_profile_requires_csrf(client, settings, provisioned_two, sign_cookie, mocked_teardown):
    client.cookies.set("krewhub_session", sign_cookie(OWNER))
    r = client.post(f"/devs/{_path(OWNER)}/profiles/close", data={"profile": "work"})
    assert r.status_code == 403
    assert mocked_teardown == []


def test_close_profile_rejects_csrf_for_a_different_profile(
    client, settings, provisioned_two, sign_cookie, mocked_teardown
):
    """CSRF é escopado POR profile: um token gerado pra fechar 'default'
    não vale pra fechar 'work'."""
    client.cookies.set("krewhub_session", sign_cookie(OWNER))
    r = client.post(
        f"/devs/{_path(OWNER)}/profiles/close",
        data={"profile": "work", "csrf": _close_csrf(settings, OWNER, "default")},
    )
    assert r.status_code == 403
    assert mocked_teardown == []


def test_close_profile_bearer_skips_csrf(client, settings, provisioned_two, sign_cookie, mocked_teardown):
    """Chamada programática (Authorization: Bearer) dispensa o token
    anti-CSRF, igual ao POST do lobby."""
    r = client.post(
        f"/devs/{_path(OWNER)}/profiles/close",
        data={"profile": "work"},
        headers={"Authorization": f"Bearer {sign_cookie(OWNER)}"},
    )
    assert r.status_code == 200
    assert mocked_teardown == [{"namespace": "krewhub-devs", "slug": "dev-a-test-local-work"}]


def test_close_profile_only_owner(client, settings, provisioned_two, sign_cookie, mocked_teardown):
    """require_owner: outro owner não fecha o pod deste (403)."""
    r = client.post(
        f"/devs/{_path(OWNER)}/profiles/close",
        data={"profile": "work"},
        headers={"Authorization": f"Bearer {sign_cookie('other@test.local')}"},
    )
    assert r.status_code == 403
    assert mocked_teardown == []


def test_close_profile_unprovisioned_is_404(client, settings, provisioned_two, sign_cookie, mocked_teardown):
    """Profile que nunca foi provisionado nesse owner -> 404 (sem linha
    no SQLite, _close_dev_session levanta ValueError)."""
    r = client.post(
        f"/devs/{_path(OWNER)}/profiles/close",
        data={"profile": "home"},
        headers={"Authorization": f"Bearer {sign_cookie(OWNER)}"},
    )
    assert r.status_code == 404
    assert mocked_teardown == []


def test_close_profile_rejects_invalid_name(client, settings, provisioned_two, sign_cookie, mocked_teardown):
    r = client.post(
        f"/devs/{_path(OWNER)}/profiles/close",
        data={"profile": "BAD!!"},
        headers={"Authorization": f"Bearer {sign_cookie(OWNER)}"},
    )
    assert r.status_code == 400
    assert mocked_teardown == []
