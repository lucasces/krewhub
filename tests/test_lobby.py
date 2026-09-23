"""GET/POST /devs/{owner_id}/lobby -- form de escolha, pular form quando
já provisionado, ?reconfigure=1, e a página final de resultado (3 links:
reconfigurar sessão / fechar sessão / sair). k8s/CHP/kirocrew 100%
mockados."""

from __future__ import annotations

from unittest import mock

import pytest

from app import chp_client, k8s_manager, kiro_login, session_client, store
from app.k8s_templates import host_for, slugify


@pytest.fixture
def mocked_infra(monkeypatch, settings):
    """Mocka reconcile_dev/wait_for_ready/register_route/issue_token_url
    (via _do_provision) e kiro_login.start_device_flow -- cobre tudo que
    _run_lobby_session encadeia, sem tocar k8s/CHP/pod real."""

    def _reconcile(_settings, owner_id):
        slug = slugify(owner_id)
        host = host_for(slug, _settings)
        return {
            "owner_id": owner_id,
            "slug": slug,
            "namespace": _settings.dev_namespace,
            "host": host,
            "steps": {"namespace": "exists", "deployment": "created"},
        }

    calls = {"reconcile": 0, "kiro_login": []}

    def _reconcile_counted(_settings, owner_id):
        calls["reconcile"] += 1
        return _reconcile(_settings, owner_id)

    def _kiro_login(_c, *, namespace, slug, mode, identity_provider=None, region=None):
        calls["kiro_login"].append(
            {"namespace": namespace, "slug": slug, "mode": mode, "identity_provider": identity_provider, "region": region}
        )
        return {"already_logged_in": False, "verification_url": "https://idp.test/device", "user_code": "WXYZ-0001"}

    monkeypatch.setattr(k8s_manager, "reconcile_dev", _reconcile_counted)
    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    monkeypatch.setattr(k8s_manager, "wait_for_ready", lambda *a, **kw: True)
    monkeypatch.setattr(chp_client, "register_route", lambda _c, _s, *, host, target: {"host": host, "target": target, "status": 201})
    monkeypatch.setattr(
        session_client,
        "issue_token_url",
        lambda _c, *, namespace, slug, host, public_port, scheme="http", ttl="24h": f"{scheme}://{host}:{public_port}/?token=tok-{slug}",
    )
    monkeypatch.setattr(kiro_login, "start_device_flow", _kiro_login)
    return calls


# ---------------------------------------------------------------------------
# GET -- form vs pular direto pro resultado
# ---------------------------------------------------------------------------


def test_get_lobby_shows_form_when_no_record_at_all(client, mocked_infra, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/devs/dev-a%40test.local/lobby")
    assert r.status_code == 200
    assert "<form" in r.text
    assert 'name="login_mode"' in r.text
    assert mocked_infra["reconcile"] == 0


def test_get_lobby_shows_form_when_record_exists_but_no_login_mode(client, mocked_infra, sign_cookie, settings):
    """Registro em `lobby_pending` (achado documentado: fica assim ANTES
    do reconcile de verdade) -- sem `login_mode`, ainda é "primeira
    vez"."""
    with store.connect(settings.db_path) as conn:
        store.upsert(
            conn, owner_id="dev-a@test.local", slug="", namespace="", host="", status="lobby_pending"
        )
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/devs/dev-a%40test.local/lobby")
    assert r.status_code == 200
    assert "<form" in r.text


def test_get_lobby_skips_form_when_login_mode_already_saved(client, mocked_infra, sign_cookie, settings):
    with store.connect(settings.db_path) as conn:
        store.set_login_choice(conn, owner_id="dev-a@test.local", mode="personal")
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/devs/dev-a%40test.local/lobby")
    assert r.status_code == 200
    assert "<form" not in r.text
    assert mocked_infra["reconcile"] == 1
    assert mocked_infra["kiro_login"][0]["mode"] == "personal"


def test_get_lobby_uses_previously_saved_org_values(client, mocked_infra, sign_cookie, settings):
    with store.connect(settings.db_path) as conn:
        store.set_login_choice(
            conn,
            owner_id="dev-a@test.local",
            mode="org",
            identity_provider="https://saved-org.example/start",
            region="saved-region-1",
        )
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/devs/dev-a%40test.local/lobby")
    assert r.status_code == 200
    call = mocked_infra["kiro_login"][0]
    assert call["mode"] == "org"
    assert call["identity_provider"] == "https://saved-org.example/start"
    assert call["region"] == "saved-region-1"


def test_get_lobby_reconfigure_forces_form_even_with_saved_login_mode(client, mocked_infra, sign_cookie, settings):
    with store.connect(settings.db_path) as conn:
        store.set_login_choice(conn, owner_id="dev-a@test.local", mode="personal")
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/devs/dev-a%40test.local/lobby?reconfigure=1")
    assert r.status_code == 200
    assert "<form" in r.text
    assert mocked_infra["reconcile"] == 0


def test_get_lobby_requires_auth(client, mocked_infra):
    r = client.get("/devs/dev-a%40test.local/lobby")
    assert r.status_code == 401


def test_get_lobby_rejects_cross_owner(client, mocked_infra, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie("dev-b@test.local"))
    r = client.get("/devs/dev-a%40test.local/lobby")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST -- validação e resolução de parâmetros
# ---------------------------------------------------------------------------


def test_post_lobby_without_login_mode_is_422(client, mocked_infra, sign_cookie):
    """ACHADO (não é bug introduzido nesta sessão, comportamento
    pré-existente): `login_mode: str = Form(...)` é campo OBRIGATÓRIO
    pro FastAPI -- omitido por completo, a validação de request do
    próprio FastAPI rejeita com 422 ANTES do handler rodar, então o
    `if login_mode not in kiro_login.MODES: 400` do código nunca é
    alcançado pra esse caso específico (só é alcançado quando o campo
    VEM só com valor inválido, ex. "bogus" -- ver teste abaixo, que
    continua 400). Diferente de `POST /kiro-login`, onde `mode` é
    `Query(None, ...)` (opcional pro FastAPI, checado manualmente) --
    por isso lá "ausente" e "inválido" dão os dois 400. Documentando a
    diferença real, não escondendo atrás de um teste ajustado às cegas."""
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/lobby", data={})
    assert r.status_code == 422


def test_post_lobby_invalid_login_mode_is_400(client, mocked_infra, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/lobby", data={"login_mode": "bogus"})
    assert r.status_code == 400


def test_post_lobby_org_without_identity_provider_or_region_is_400(client, mocked_infra, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/lobby", data={"login_mode": "org"})
    assert r.status_code == 400
    assert "identity_provider" in r.json()["detail"]


def test_post_lobby_org_with_form_values_succeeds_and_persists(client, mocked_infra, sign_cookie, settings):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post(
        "/devs/dev-a%40test.local/lobby",
        data={
            "login_mode": "org",
            "identity_provider": "https://form-value.example/start",
            "region": "form-region-1",
        },
    )
    assert r.status_code == 200
    call = mocked_infra["kiro_login"][0]
    assert call["mode"] == "org"
    assert call["identity_provider"] == "https://form-value.example/start"
    assert call["region"] == "form-region-1"

    with store.connect(settings.db_path) as conn:
        row = store.get(conn, "dev-a@test.local")
    assert row["login_mode"] == "org"
    assert row["login_identity_provider"] == "https://form-value.example/start"
    assert row["login_region"] == "form-region-1"


def test_post_lobby_personal_ignores_identity_provider_and_region_even_if_sent(client, mocked_infra, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post(
        "/devs/dev-a%40test.local/lobby",
        data={"login_mode": "personal", "identity_provider": "should-be-ignored", "region": "should-be-ignored"},
    )
    assert r.status_code == 200
    call = mocked_infra["kiro_login"][0]
    assert call["mode"] == "personal"
    assert call["identity_provider"] is None
    assert call["region"] is None


def test_post_lobby_requires_auth(client, mocked_infra):
    r = client.post("/devs/dev-a%40test.local/lobby", data={"login_mode": "personal"})
    assert r.status_code == 401


def test_post_lobby_rejects_cross_owner(client, mocked_infra, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie("dev-b@test.local"))
    r = client.post("/devs/dev-a%40test.local/lobby", data={"login_mode": "personal"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Página de resultado -- os 3 links
# ---------------------------------------------------------------------------


def test_result_page_has_reconfigure_close_and_logout_links(client, mocked_infra, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/lobby", data={"login_mode": "personal"})
    assert r.status_code == 200
    assert 'href="/devs/dev-a@test.local/lobby?reconfigure=1"' in r.text
    assert 'href="/close"' in r.text
    assert 'href="/logout"' in r.text
    assert "Reconfigurar sess" in r.text
    assert "Fechar sess" in r.text
    assert "Sair" in r.text


def test_result_page_shows_already_logged_in_message(client, mocked_infra, sign_cookie, monkeypatch):
    monkeypatch.setattr(
        kiro_login,
        "start_device_flow",
        lambda *_a, **_kw: {"already_logged_in": True, "whoami": "logged in as dev-a"},
    )
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/lobby", data={"login_mode": "personal"})
    assert r.status_code == 200
    assert "já está logado" in r.text or "ja esta logado" in r.text.lower()


def test_result_page_shows_device_flow_link_when_not_logged_in(client, mocked_infra, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/lobby", data={"login_mode": "personal"})
    assert "https://idp.test/device" in r.text
    assert "WXYZ-0001" in r.text
