"""Integração das extensões em app/main.py: lobby, endpoints, provision e
limpeza em /close e /logout (k8s e pod mockados)."""

from __future__ import annotations

import dataclasses
from unittest import mock

import pytest

from app import chp_client, k8s_manager, kiro_login, session_client, store
from app import k8s_templates as tpl
from app.extensions import base, runtime
from tests.ext_demo import demo_installed, enable_demo, fake_clients, pod  # noqa: F401
from tests.test_close_logout import mocked_revoke, mocked_teardown, provisioned  # noqa: F401

OWNER = "dev-a@test.local"
URL = "/devs/dev-a%40test.local"
HREF = "/devs/dev-a@test.local"


@pytest.fixture
def ext_client(client, settings, demo_installed, monkeypatch):
    import app.main as main

    s = dataclasses.replace(settings, extensions_enabled="demo")
    monkeypatch.setattr(main, "_settings", s)
    return client, s


@pytest.fixture
def login(sign_cookie):
    def _login(client):
        client.cookies.set("krewhub_session", sign_cookie(OWNER))

    return _login


@pytest.fixture
def infra(monkeypatch, fake_clients):  # noqa: F811
    calls = {"reconcile": [], "ready": []}

    def _reconcile(_s, owner_id, plans=()):
        calls["reconcile"].append(plans)
        slug = tpl.slugify(owner_id)
        return {"owner_id": owner_id, "slug": slug, "namespace": _s.dev_namespace,
                "host": f"{slug}.kiro.internal", "steps": {}}

    monkeypatch.setattr(k8s_manager, "reconcile_dev", _reconcile)
    monkeypatch.setattr(k8s_manager, "wait_for_ready", lambda *a, **kw: True)
    monkeypatch.setattr(chp_client, "register_route", lambda *a, **kw: {"status": 201})
    monkeypatch.setattr(session_client, "issue_token_url", lambda *a, **kw: "http://x/?token=t")
    monkeypatch.setattr(
        kiro_login, "start_device_flow",
        lambda *a, **kw: {"already_logged_in": False, "verification_url": "https://idp.test/d", "user_code": "AB-1"},
    )
    monkeypatch.setattr(runtime, "on_pod_ready_best_effort", lambda *a, **kw: calls["ready"].append(1))
    return calls


FORM = {
    "login_mode": "personal",
    "ext.demo.enabled": "on",
    "ext.demo.url": "https://demo.test",
    "ext.demo.mode": "b",
    "ext.demo.token": "s3cret",
}


# --- lobby --------------------------------------------------------------


def test_lobby_form_lists_enabled_extensions(ext_client, login, infra):
    client, _ = ext_client
    login(client)
    r = client.get(f"{URL}/lobby")
    assert 'name="ext.demo.enabled"' in r.text and 'name="ext.demo.token"' in r.text


def test_lobby_form_without_admin_enabled_extensions_has_no_section(client, login, infra, demo_installed):
    login(client)
    assert "ext.demo" not in client.get(f"{URL}/lobby").text


def test_lobby_post_saves_config_passes_plans_and_shows_cards_iframe(ext_client, login, infra, fake_clients):
    client, s = ext_client
    login(client)
    r = client.post(f"{URL}/lobby", data=FORM)
    assert r.status_code == 200
    assert f'src="{HREF}/extensions/cards"' in r.text
    (plans,) = infra["reconcile"]
    assert [p.ext_id for p in plans] == ["demo"]
    assert infra["ready"] == [1]
    with store.connect(s.db_path) as conn:
        row = store.get_extension(conn, OWNER, "demo")
    assert row["enabled"] and row["config"]["mode"] == "b"
    assert "s3cret" not in str(row)
    assert fake_clients.core.create_namespaced_secret.call_args.args[1]["stringData"] == {"demo.token": "s3cret"}


def test_lobby_post_with_invalid_extension_config_saves_nothing(ext_client, login, infra, fake_clients):
    client, s = ext_client
    login(client)
    r = client.post(f"{URL}/lobby", data={**FORM, "ext.demo.url": "http://insecure"})
    assert r.status_code == 400
    assert "URL" in r.text
    assert infra["reconcile"] == []
    fake_clients.core.create_namespaced_secret.assert_not_called()
    with store.connect(s.db_path) as conn:
        assert store.get_extension(conn, OWNER, "demo") is None


def test_lobby_post_without_extensions_does_not_pass_plans(client, login, infra, demo_installed):
    login(client)
    r = client.post(f"{URL}/lobby", data={"login_mode": "personal"})
    assert r.status_code == 200
    assert infra["reconcile"] == [()]
    assert "iframe" not in r.text


def test_provision_maps_contribution_conflicts_to_422(ext_client, login, infra, monkeypatch):
    client, s = ext_client
    login(client)
    from app.extensions.contributions import ContributionError

    def boom(*a, **kw):
        raise ContributionError("conflito")

    monkeypatch.setattr(runtime, "build_plans", boom)
    r = client.post(f"{URL}/lobby", data=FORM)
    assert r.status_code == 422


# --- endpoints ----------------------------------------------------------


def _ready(fake_clients):
    fake_clients.core.read_namespaced_pod.side_effect = None
    fake_clients.core.read_namespaced_pod.return_value = pod()


def test_extensions_json_requires_the_owner(ext_client, login, fake_clients):
    client, _ = ext_client
    assert client.get(f"{URL}/extensions").status_code == 401
    login(client)
    assert client.get("/devs/dev-b%40test.local/extensions").status_code == 403
    r = client.get(f"{URL}/extensions")
    assert r.status_code == 200
    assert r.json()["extensions"][0]["state"] == "inactive"


def test_extensions_cards_render_with_refresh_while_pending(ext_client, login, fake_clients):
    client, s = ext_client
    enable_demo(s)
    login(client)
    r = client.get(f"{URL}/extensions/cards")
    assert 'http-equiv="refresh"' in r.text
    _ready(fake_clients)
    r = client.get(f"{URL}/extensions/cards")
    assert 'http-equiv="refresh"' not in r.text
    assert f'action="{HREF}/extensions/demo/actions/login"' in r.text
    assert 'name="csrf"' in r.text


def _token(s, action):
    return runtime.make_csrf(s.session_secret, OWNER, "demo", action)


def test_action_requires_csrf_for_cookie_sessions(ext_client, login, fake_clients):
    client, s = ext_client
    enable_demo(s)
    _ready(fake_clients)
    login(client)
    assert client.post(f"{URL}/extensions/demo/actions/login", data={}).status_code == 403
    bad = runtime.make_csrf(s.session_secret, OWNER, "demo", "sync")  # token de outra ação
    assert client.post(f"{URL}/extensions/demo/actions/login", data={"csrf": bad}).status_code == 403


def test_action_with_csrf_redirects_browsers_back_to_cards(ext_client, login, fake_clients):
    client, s = ext_client
    enable_demo(s)
    _ready(fake_clients)
    login(client)
    r = client.post(
        f"{URL}/extensions/demo/actions/login",
        data={"csrf": _token(s, "login")},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == f"{HREF}/extensions/cards"
    with store.connect(s.db_path) as conn:
        assert store.get_extension(conn, OWNER, "demo")["runtime_state"]["authed"] is True


def test_action_with_bearer_returns_json_without_csrf(ext_client, sign_cookie, fake_clients):
    client, s = ext_client
    enable_demo(s)
    _ready(fake_clients)
    r = client.post(
        f"{URL}/extensions/demo/actions/login",
        headers={"authorization": f"Bearer {sign_cookie(OWNER)}"},
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "message": "logado"}


def test_action_collects_repeated_checkbox_fields_into_a_multiselect(ext_client, login, fake_clients):
    client, s = ext_client
    enable_demo(s)
    _ready(fake_clients)
    login(client)
    r = client.post(
        f"{URL}/extensions/demo/actions/pick",
        data={"csrf": _token(s, "pick"), "items": ["a", "b"]},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 200 and r.json()["message"] == "pick a,b"
    r = client.post(f"{URL}/extensions/demo/actions/pick", data={"csrf": _token(s, "pick"), "items": "zzz"})
    assert r.status_code == 400


def test_action_rejections_map_to_http_errors(ext_client, login, fake_clients):
    client, s = ext_client
    enable_demo(s)
    _ready(fake_clients)
    login(client)
    r = client.post(f"{URL}/extensions/demo/actions/nope", data={"csrf": _token(s, "nope")})
    assert r.status_code == 404
    r = client.post(f"{URL}/extensions/demo/actions/sync", data={"csrf": _token(s, "sync")})
    assert r.status_code == 409


# --- /close e /logout ---------------------------------------------------


def _secret(keys):
    sec = mock.Mock()
    sec.data = {k: "dg==" for k in keys}
    return sec


@pytest.fixture
def with_secret(ext_client, mocked_revoke, mocked_teardown, provisioned, fake_clients):  # noqa: F811
    client, s = ext_client
    enable_demo(s)
    fake_clients.core.read_namespaced_secret.side_effect = None
    fake_clients.core.read_namespaced_secret.return_value = _secret(["demo.token", "demo.auth"])
    return client, s, fake_clients


def test_logout_wipes_all_extension_secret_keys(with_secret, login):
    client, s, fc = with_secret
    login(client)
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 302
    call = fc.core.patch_namespaced_secret.call_args
    assert call.args[2] == {"data": {"demo.auth": None, "demo.token": None}}
    assert call.kwargs == {"_content_type": "application/merge-patch+json"}
    fc.core.delete_namespaced_secret.assert_not_called()
    fc.core.delete_namespaced_config_map.assert_called_once()


def test_close_wipes_only_generated_keys(with_secret, login):
    client, s, fc = with_secret
    login(client)
    assert client.get("/close").status_code == 200
    assert fc.core.patch_namespaced_secret.call_args.args[2] == {"data": {"demo.auth": None}}


def test_close_wipes_even_when_teardown_fails(with_secret, login, monkeypatch):
    client, s, fc = with_secret
    monkeypatch.setattr(
        k8s_manager, "teardown_dev_workload",
        mock.Mock(side_effect=k8s_manager.TeardownError("falhou")),
    )
    login(client)
    assert client.get("/close").status_code == 502
    assert fc.core.patch_namespaced_secret.call_args.args[2] == {"data": {"demo.auth": None}}


def test_logout_wipes_even_if_owner_was_never_provisioned(ext_client, login, mocked_revoke, fake_clients):
    client, s = ext_client
    fake_clients.core.read_namespaced_secret.side_effect = None
    fake_clients.core.read_namespaced_secret.return_value = _secret(["demo.token"])
    login(client)
    assert client.get("/logout", follow_redirects=False).status_code == 302
    assert fake_clients.core.patch_namespaced_secret.call_args.args[2] == {"data": {"demo.token": None}}


def test_close_without_extensions_never_touches_extension_resources(
    client, login, provisioned, mocked_revoke, mocked_teardown, monkeypatch, demo_installed
):
    spy = mock.Mock()
    monkeypatch.setattr(k8s_manager, "teardown_ext_resources", spy)
    login(client)
    assert client.get("/close").status_code == 200
    spy.assert_not_called()


def test_cleanup_failure_does_not_break_close(with_secret, login, monkeypatch):
    client, s, fc = with_secret
    fc.core.patch_namespaced_secret.side_effect = RuntimeError("k8s caiu")
    login(client)
    assert client.get("/close").status_code == 200


@pytest.mark.parametrize("polling", [True, False])
def test_extensions_cards_refresh_while_an_extension_is_waiting_on_something_external(
    ext_client, login, fake_clients, monkeypatch, polling  # noqa: F811
):
    """Regressão: o estado "esperando o dev autorizar no portal" é
    `needs_action`, nunca `pending`, e a página não recarregava. Mas
    `needs_action` sem espera externa (ex.: dev escolhendo um papel) NÃO
    pode recarregar, senão apaga o que ele digita."""
    from tests.ext_demo import DemoExtension

    def status(self, ctx):
        card = base.Card(title="Demo", state="needs_action", summary="aguardando", polling=polling)
        return base.Status(conditions={}, card=card, state="needs_action")

    monkeypatch.setattr(DemoExtension, "status", status)
    client, s = ext_client
    enable_demo(s)
    _ready(fake_clients)
    login(client)
    r = client.get(f"{URL}/extensions/cards")
    assert r.status_code == 200 and "aguardando" in r.text
    assert ('http-equiv="refresh"' in r.text) is polling
