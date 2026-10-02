"""Runtime das extensões: form, planos, estados, ações, limpeza, CSRF e UI."""

from __future__ import annotations

import dataclasses
from unittest import mock

import pytest
from kubernetes.client.rest import ApiException

from app import extensions, k8s_manager, store
from app.extensions import runtime, ui
from tests.ext_demo import (  # noqa: F401
    demo_installed,
    enable_demo,
    ext_settings,
    fake_clients,
    pod,
    ready_pod,
)

OWNER = "dev-a@test.local"


def _form(**kw):
    base = {"ext.demo.enabled": "on", "ext.demo.url": "https://demo.test", "ext.demo.mode": "a",
            "ext.demo.token": "s3cret"}
    base.update(kw)
    return base


# --- form --------------------------------------------------------------


def test_parse_form_collects_config_and_secret_values(ext_settings):
    changes, errors = runtime.parse_form(ext_settings, OWNER, _form(**{"ext.demo.flag": "on"}))
    assert errors == {}
    assert changes["demo"]["enabled"] is True
    assert changes["demo"]["config"] == {"url": "https://demo.test", "mode": "a", "flag": True}
    assert changes["demo"]["secret_values"] == {"demo.token": "s3cret"}


def test_parse_form_ignores_extensions_not_enabled_by_admin(settings, demo_installed):
    changes, errors = runtime.parse_form(settings, OWNER, _form())
    assert changes == {} and errors == {}


def test_parse_form_reports_validation_and_missing_required_secret(ext_settings):
    _, errors = runtime.parse_form(
        ext_settings, OWNER, _form(**{"ext.demo.url": "http://x", "ext.demo.token": ""})
    )
    assert any("URL" in m for m in errors["demo"])
    assert any("Token" in m for m in errors["demo"])


def test_parse_form_keeps_existing_secret_when_blank(ext_settings):
    enable_demo(ext_settings)
    _, errors = runtime.parse_form(ext_settings, OWNER, _form(**{"ext.demo.token": ""}))
    assert errors == {}


def test_disabled_extension_is_not_validated(ext_settings):
    changes, errors = runtime.parse_form(ext_settings, OWNER, {"ext.demo.url": "bad"})
    assert errors == {} and changes["demo"]["enabled"] is False


def test_save_form_writes_secret_to_k8s_and_only_markers_to_sqlite(ext_settings, fake_clients):
    changes, _ = runtime.parse_form(ext_settings, OWNER, _form())
    runtime.save_form(ext_settings, OWNER, changes)
    _, body = fake_clients.core.create_namespaced_secret.call_args.args
    assert body["stringData"] == {"demo.token": "s3cret"}
    with store.connect(ext_settings.db_path) as conn:
        row = store.get_extension(conn, OWNER, "demo")
        raw = conn.execute("SELECT config_json, runtime_state_json FROM dev_extensions").fetchone()
    assert row["enabled"] and row["secrets"]["token"]["set"] is True
    assert "s3cret" not in "".join(raw)


def test_disabling_wipes_the_extension_keys(ext_settings, fake_clients):
    changes, _ = runtime.parse_form(ext_settings, OWNER, _form())
    runtime.save_form(ext_settings, OWNER, changes)
    sec = mock.Mock()
    sec.data = {"demo.token": "x", "demo.auth": "y", "other.k": "z"}
    fake_clients.core.read_namespaced_secret.side_effect = None
    fake_clients.core.read_namespaced_secret.return_value = sec
    off, _ = runtime.parse_form(ext_settings, OWNER, {})
    runtime.save_form(ext_settings, OWNER, off)
    call = fake_clients.core.patch_namespaced_secret.call_args
    assert call.args[2] == {"data": {"demo.token": None, "demo.auth": None}}
    with store.connect(ext_settings.db_path) as conn:
        row = store.get_extension(conn, OWNER, "demo")
    assert row["enabled"] is False and row["secrets"] == {}


# --- planos -------------------------------------------------------------


def test_build_plans_only_for_active_extensions(ext_settings):
    assert runtime.build_plans(ext_settings, OWNER) == ()
    enable_demo(ext_settings)
    (plan,) = runtime.build_plans(ext_settings, OWNER)
    assert plan.ext_id == "demo" and plan.generated_keys == ("demo.auth",)
    assert plan.contribution.containers[0]["name"] == "demo"


def test_build_plans_skips_invalid_config(ext_settings):
    enable_demo(ext_settings, config={"url": "http://not-https"})
    assert runtime.build_plans(ext_settings, OWNER) == ()


# --- evaluate -----------------------------------------------------------


def test_evaluate_inactive_when_dev_has_not_enabled(ext_settings, fake_clients):
    (v,) = runtime.evaluate(ext_settings, OWNER, c=fake_clients)
    assert v.state == "inactive"
    fake_clients.core.read_namespaced_pod.assert_not_called()


def test_evaluate_pending_when_pod_not_ready(ext_settings, fake_clients):
    enable_demo(ext_settings)
    fake_clients.core.read_namespaced_pod.side_effect = None
    fake_clients.core.read_namespaced_pod.return_value = pod(ready=False)
    (v,) = runtime.evaluate(ext_settings, OWNER, c=fake_clients)
    assert v.state == "pending"
    assert all(not a.enabled for a in v.actions)


def test_evaluate_pending_when_pod_missing(ext_settings, fake_clients):
    enable_demo(ext_settings)
    (v,) = runtime.evaluate(ext_settings, OWNER, c=fake_clients)
    assert v.state == "pending"


def test_evaluate_needs_action_then_ready(ext_settings, ready_pod):
    enable_demo(ext_settings)
    (v,) = runtime.evaluate(ext_settings, OWNER, c=ready_pod)
    assert v.state == "needs_action"
    assert v.conditions == {"pod.ready": True, "sidecar.demo.running": True, "demo.authenticated": False}
    assert {a.id: a.enabled for a in v.actions} == {"login": True, "sync": False}
    with store.connect(ext_settings.db_path) as conn:
        store.upsert_extension(conn, OWNER, "demo", runtime_state={"authed": True})
    (v,) = runtime.evaluate(ext_settings, OWNER, c=ready_pod)
    assert v.state == "ready" and v.card.state == "ready"


def test_evaluate_isolates_hook_exceptions(settings, demo_installed, ready_pod):
    s = dataclasses.replace(settings, extensions_enabled="boom")
    with store.connect(s.db_path) as conn:
        store.upsert_extension(conn, OWNER, "boom", enabled=True, config={"url": "https://x.test"})
    (v,) = runtime.evaluate(s, OWNER, c=ready_pod)
    assert v.state == "error"
    assert "segredo-interno" not in str(v.card)


def test_evaluate_invalid_config_is_error(ext_settings, ready_pod):
    enable_demo(ext_settings, config={"url": "http://bad"})
    (v,) = runtime.evaluate(ext_settings, OWNER, c=ready_pod)
    assert v.state == "error" and v.card.messages


# --- ações --------------------------------------------------------------


def test_run_action_updates_state_and_records_last_action(ext_settings, ready_pod):
    enable_demo(ext_settings)
    res = runtime.run_action(ext_settings, OWNER, "demo", "login", {})
    assert res.ok
    (v,) = runtime.evaluate(ext_settings, OWNER, c=ready_pod)
    assert v.state == "ready"
    assert "login" in v.last_action and "ok" in v.last_action


def test_run_action_rejections(ext_settings, ready_pod):
    with pytest.raises(runtime.ActionRejected) as e:
        runtime.run_action(ext_settings, OWNER, "ghost", "login", {})
    assert e.value.status_code == 404
    with pytest.raises(runtime.ActionRejected) as e:
        runtime.run_action(ext_settings, OWNER, "demo", "nope", {})
    assert e.value.status_code == 404
    with pytest.raises(runtime.ActionRejected) as e:  # extensão desligada
        runtime.run_action(ext_settings, OWNER, "demo", "login", {})
    assert e.value.status_code == 409
    enable_demo(ext_settings)
    with pytest.raises(runtime.ActionRejected) as e:  # requires não atendido
        runtime.run_action(ext_settings, OWNER, "demo", "sync", {})
    assert e.value.status_code == 409


def test_run_action_validates_params(ext_settings, ready_pod):
    enable_demo(ext_settings)
    with store.connect(ext_settings.db_path) as conn:
        store.upsert_extension(conn, OWNER, "demo", runtime_state={"authed": True})
    with pytest.raises(runtime.ActionRejected, match="scope"):
        runtime.run_action(ext_settings, OWNER, "demo", "sync", {"scope": "z"})
    assert runtime.run_action(ext_settings, OWNER, "demo", "sync", {"scope": "y"}).message == "sync y"


def test_run_action_hides_internal_errors(settings, demo_installed, ready_pod):
    s = dataclasses.replace(settings, extensions_enabled="boom")
    with store.connect(s.db_path) as conn:
        store.upsert_extension(conn, OWNER, "boom", enabled=True, config={"url": "https://x.test"})
    ready_pod.core.read_namespaced_pod.return_value = pod(sidecars=("boom",))
    # estado `error` não bloqueia a ação (permite tentar de novo); a
    # exceção interna vira ok=False sem vazar o texto original.
    res = runtime.run_action(s, OWNER, "boom", "login", {})
    assert res.ok is False
    assert "segredo-interno" not in res.message


def test_on_pod_ready_best_effort_swallows_errors(settings, demo_installed, ready_pod):
    s = dataclasses.replace(settings, extensions_enabled="boom")
    with store.connect(s.db_path) as conn:
        store.upsert_extension(conn, OWNER, "boom", enabled=True, config={"url": "https://x.test"})
    runtime.on_pod_ready_best_effort(s, OWNER, ready_pod)


# --- limpeza ------------------------------------------------------------


def _secret(keys):
    s = mock.Mock()
    s.data = {k: "dg==" for k in keys}
    return s


def test_wipe_secrets_logout_removes_everything(ext_settings, fake_clients):
    enable_demo(ext_settings)
    fake_clients.core.read_namespaced_secret.side_effect = None
    fake_clients.core.read_namespaced_secret.return_value = _secret(["demo.token", "demo.auth"])
    wiped = runtime.wipe_secrets(ext_settings, OWNER, generated_only=False, c=fake_clients)
    assert sorted(wiped) == ["demo.auth", "demo.token"]
    call = fake_clients.core.patch_namespaced_secret.call_args
    assert call.args[2] == {"data": {"demo.auth": None, "demo.token": None}}
    assert call.kwargs == {"_content_type": "application/merge-patch+json"}
    with store.connect(ext_settings.db_path) as conn:
        row = store.get_extension(conn, OWNER, "demo")
    assert row["secrets"] == {} and row["runtime_state"] == {}
    assert row["enabled"] is True


def test_wipe_secrets_close_keeps_user_provided_secrets(ext_settings, fake_clients):
    enable_demo(ext_settings)
    fake_clients.core.read_namespaced_secret.side_effect = None
    fake_clients.core.read_namespaced_secret.return_value = _secret(["demo.token", "demo.auth"])
    wiped = runtime.wipe_secrets(ext_settings, OWNER, generated_only=True, c=fake_clients)
    assert wiped == ["demo.auth"]
    assert fake_clients.core.patch_namespaced_secret.call_args.args[2] == {"data": {"demo.auth": None}}
    with store.connect(ext_settings.db_path) as conn:
        assert store.get_extension(conn, OWNER, "demo")["secrets"]["token"]["set"] is True


def test_wipe_secrets_without_secret_object_is_noop(ext_settings, fake_clients):
    assert runtime.wipe_secrets(ext_settings, OWNER, generated_only=False, c=fake_clients) == []
    fake_clients.core.patch_namespaced_secret.assert_not_called()


# --- CSRF ---------------------------------------------------------------


def test_csrf_roundtrip_and_binding():
    tok = runtime.make_csrf("k", "o", "e", "a", now=1000)
    assert runtime.verify_csrf("k", tok, "o", "e", "a", now=1001)
    assert not runtime.verify_csrf("k", tok, "o2", "e", "a", now=1001)
    assert not runtime.verify_csrf("k", tok, "o", "e", "b", now=1001)
    assert not runtime.verify_csrf("other", tok, "o", "e", "a", now=1001)
    assert not runtime.verify_csrf("k", tok, "o", "e", "a", now=1000 + runtime.CSRF_TTL_SECONDS + 1)
    assert not runtime.verify_csrf("k", "garbage", "o", "e", "a")
    assert not runtime.verify_csrf("k", "", "o", "e", "a")


# --- UI -----------------------------------------------------------------


def test_config_section_never_prefills_secret_and_escapes(ext_settings):
    exts = extensions.enabled_extensions(ext_settings)
    row = {"enabled": True, "config": {"url": '"><script>x</script>'}, "secrets": {"token": {"set": True}}}
    out = ui.render_config_section([(exts["demo"], row, ["<b>erro</b>"])])
    assert "<script>" not in out and "<b>erro</b>" not in out
    assert 'name="ext.demo.token"' in out and 'type="password"' in out
    assert 'value="" placeholder="já definido' in out
    assert 'name="ext.demo.enabled" checked' in out
    assert ui.render_config_section([]) == ""


def test_card_rendering_escapes_and_filters_links():
    card = extensions.base.Card(
        title="<i>t</i>", summary="<script>s</script>", code="A<B",
        links=(extensions.base.Link("ok", "https://a.test/x?a=1&b=2"),
               extensions.base.Link("bad", "javascript:alert(1)")),
        rows=(("k<", "v>"),), messages=("<m>",),
    )
    view = ui.CardView("demo", "Demo", "ready", card,
                       (ui.ActionButton("login", "Entrar<", True),))
    out = ui.render_card("a@b.c", view, lambda e, a: "TOK")
    assert "<script>" not in out and "javascript:" not in out
    assert 'href="https://a.test/x?a=1&amp;b=2"' in out
    assert 'action="/devs/a@b.c/extensions/demo/actions/login"' in out
    assert 'name="csrf" value="TOK"' in out


def test_cards_document_refresh_only_when_requested():
    assert 'http-equiv="refresh"' not in ui.render_cards_document("o", [], lambda e, a: "")
    assert 'content="5"' in ui.render_cards_document("o", [], lambda e, a: "", refresh_seconds=5)


@pytest.mark.parametrize(
    "state,polling,expected",
    [
        ("pending", False, True),
        ("needs_action", True, True),
        ("degraded", True, True),
        ("needs_action", False, False),  # formulário de ação aberto: não pode recarregar
        ("degraded", False, False),
        ("ready", False, False),
        ("inactive", False, False),
        ("error", False, False),
    ],
)
def test_wants_refresh_follows_pending_or_the_extensions_polling_flag(state, polling, expected):
    from types import SimpleNamespace

    from app.extensions.base import Card

    view = SimpleNamespace(state=state, card=Card(title="x", state=state, polling=polling))
    assert runtime.wants_refresh([view]) is expected


def test_wants_refresh_is_true_when_any_extension_wants_it():
    from types import SimpleNamespace

    from app.extensions.base import Card

    quiet = SimpleNamespace(state="ready", card=Card(title="a"))
    waiting = SimpleNamespace(state="needs_action", card=Card(title="b", polling=True))
    assert runtime.wants_refresh([quiet, waiting]) is True
    assert runtime.wants_refresh([]) is False
