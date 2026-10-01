"""Extensão AWS SSO (pacote `extensions/aws-sso`) + supervisor do sidecar.
Nenhum cluster nem AWS: o exec do Pod é simulado e o `aws-sso` é um
script falso."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from app import extensions
from app.extensions import base
from app.extensions.contributions import ContributionError
from app.k8s_templates import build_pod
from krewhub_ext_aws_sso import AwsSsoExtension, render_config
from tests.test_k8s_manager import _settings

ROOT = Path(__file__).resolve().parent.parent
CFG = {
    "start_url": "https://d-0123456789.awsapps.com/start",
    "sso_region": "us-east-1",
    "default_region": "sa-east-1",
}


def _ctx(exec_fn, *, state=None, base_conditions=None, detached=None):
    return base.ExtensionContext(
        owner_id="dev@test.local",
        slug="dev-test-local",
        namespace="ns",
        pod_name="kirocrew-dev-test-local",
        config=CFG,
        state=state if state is not None else {},
        base_conditions=base_conditions or {"pod.ready": True, "sidecar.aws-sso.running": True},
        settings=None,
        container_default="aws-sso",
        _exec=lambda container, script: exec_fn(script),
        _get_secret=lambda key: None,
        _run_detached=detached,
    )


# --- descoberta e configuração ------------------------------------------------


def test_entry_point_is_discovered_and_definition_is_valid():
    found = extensions.discover(refresh=True)
    assert isinstance(found["aws-sso"], AwsSsoExtension)
    found["aws-sso"].check_definition()


def test_validate_rejects_bad_url_and_region():
    ext = AwsSsoExtension()
    assert ext.validate(CFG) == []
    errors = ext.validate({"start_url": "http://x", "sso_region": "mars", "default_region": ""})
    assert len(errors) == 2
    assert any("URL" in e for e in errors)
    assert ext.validate({}) != []


def test_render_config_is_device_code_with_global_auth_workflow():
    text = render_config(CFG)
    assert 'StartUrl: "https://d-0123456789.awsapps.com/start"' in text
    assert 'DefaultRegion: "sa-east-1"' in text
    assert "UrlAction: print" in text
    assert "SecureStore: json" in text
    # nível global (dentro de SSOConfig o aws-sso ignora a chave)
    assert "\nAuthWorkflow: device_code\n" in text
    assert render_config({**CFG, "default_region": ""}).count("us-east-1") == 2


def test_default_region_falls_back_to_sso_region():
    ext = AwsSsoExtension()
    ctx = base.BuildContext("dev@test.local", "dev-test-local", "ns", {**CFG, "default_region": ""}, None)
    env = {e["name"]: e.get("value") for e in ext.pod_contribution(ctx).main_env}
    assert env["AWS_REGION"] == "us-east-1"


# --- Pod ------------------------------------------------------------------


def _pod(monkeypatch, image=None):
    if image:
        monkeypatch.setenv("KREWHUB_EXT_AWS_SSO_IMAGE", image)
    ext = AwsSsoExtension()
    ctx = base.BuildContext("dev@test.local", "dev-test-local", "ns", CFG, None)
    return build_pod("ns", "dev-test-local", _settings(), [("aws-sso", ext.pod_contribution(ctx))])


def test_pod_has_hardened_sidecar_and_private_state(monkeypatch):
    pod = _pod(monkeypatch, "registry.test/krewhub-ext-aws-sso:9")
    spec = pod["spec"]
    main, side = spec["containers"]
    assert (main["name"], side["name"]) == ("kirocrew", "aws-sso")
    assert side["image"] == "registry.test/krewhub-ext-aws-sso:9"
    assert side["securityContext"]["readOnlyRootFilesystem"] is True
    assert side["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert "readinessProbe" not in side

    mounts = {m["mountPath"]: m for m in side["volumeMounts"]}
    assert set(mounts) == {"/state", "/tmp", "/etc/aws-sso"}
    assert mounts["/etc/aws-sso"]["readOnly"] is True
    # o token SSO vive no emptyDir privado: o kirocrew não o monta
    assert "aws-sso-state" not in {m["name"] for m in main["volumeMounts"]}
    vols = {v["name"]: v for v in spec["volumes"]}
    assert vols["aws-sso-state"] == {"name": "aws-sso-state", "emptyDir": {}}
    assert vols["aws-sso-files"]["configMap"]["items"] == [{"key": "aws-sso.config.yaml", "path": "config.yaml"}]


def test_pod_main_env_points_sdk_at_the_sidecar_with_the_bearer(monkeypatch):
    pod = _pod(monkeypatch)
    main = pod["spec"]["containers"][0]
    names = [e["name"] for e in main["env"]]
    # a referência $(VAR) só expande se a variável vier ANTES na lista
    assert names.index("KREWHUB_AWS_SSO_TOKEN") < names.index("AWS_CONTAINER_AUTHORIZATION_TOKEN")
    env = {e["name"]: e for e in main["env"]}
    assert env["AWS_CONTAINER_AUTHORIZATION_TOKEN"]["value"] == "Bearer $(KREWHUB_AWS_SSO_TOKEN)"
    assert env["AWS_CONTAINER_CREDENTIALS_FULL_URI"]["value"] == "http://127.0.0.1:4144/"
    ref = env["KREWHUB_AWS_SSO_TOKEN"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "krewhub-ext-dev-test-local", "key": "aws-sso.bearer"}
    assert "value" not in env["KREWHUB_AWS_SSO_TOKEN"]


def test_pod_spec_hash_changes_with_config(monkeypatch):
    ext = AwsSsoExtension()

    def hash_for(cfg):
        ctx = base.BuildContext("dev@test.local", "dev-test-local", "ns", cfg, None)
        pod = build_pod("ns", "dev-test-local", _settings(), [("aws-sso", ext.pod_contribution(ctx))])
        return pod["metadata"]["annotations"]["krewhub.pespa.net/spec-hash"]
    assert hash_for(CFG) == hash_for(dict(CFG))
    assert hash_for(CFG) != hash_for({**CFG, "sso_region": "eu-west-1"})


# --- status -------------------------------------------------------------------


def _status(payload, **kw):
    out = json.dumps(payload) if payload is not None else ""
    return AwsSsoExtension().status(_ctx(lambda script: out, **kw))


def test_status_needs_login_shows_pending_device_code_link():
    import time
    state = {"login": {"url": "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-EFGH", "code": "ABCD-EFGH", "at": time.time()}}
    st = _status({"server": True, "logged_in": False}, state=state)
    assert st.state == "needs_action"
    assert st.card.code == "ABCD-EFGH"
    assert st.card.links[0].url.startswith("https://device.sso")


def test_status_ignores_stale_login_link():
    state = {"login": {"url": "https://x.amazonaws.com/", "code": "A", "at": 1.0}}
    st = _status({"server": True, "logged_in": False}, state=state)
    assert st.card.links == () and st.card.code == ""


def test_status_logged_in_without_role_then_ready():
    st = _status({"server": True, "logged_in": True, "roles": 2, "role_names": ["a:Admin", "b:Dev"], "profile": ""})
    assert st.state == "needs_action"
    assert "a:Admin" in " ".join(st.card.messages)
    st = _status({"server": True, "logged_in": True, "profile": "a:Admin", "loaded": True})
    assert st.state == "ready"
    assert ("Papel", "a:Admin") in st.card.rows
    assert st.conditions["sso.role_selected"] and st.conditions["sso.creds_loaded"]


def test_status_pending_when_sidecar_not_running_and_tolerates_garbage():
    st = _status(None, base_conditions={"pod.ready": True, "sidecar.aws-sso.running": False})
    assert st.state == "needs_action" or st.state == "pending"
    ctx = _ctx(lambda script: "not json{")
    assert AwsSsoExtension().status(ctx).conditions["sso.logged_in"] is False


def test_status_surfaces_supervisor_error():
    st = _status({"server": True, "logged_in": False, "error": "Sessão SSO expirada"})
    assert "Sessão SSO expirada" in st.card.messages


# --- ações --------------------------------------------------------------------


def test_apply_roles_rejects_shell_metacharacters():
    ext = AwsSsoExtension()
    ran: list[str] = []
    ctx = _ctx(lambda s: ran.append(s) or "")
    with pytest.raises(base.ExtensionError):
        ext.handle_action(ctx, "apply_roles", {"profile": "x; rm -rf /"})
    assert ran == []


def test_apply_roles_writes_quoted_request():
    ran: list[str] = []
    ext = AwsSsoExtension()
    res = ext.handle_action(_ctx(lambda s: ran.append(s) or ""), "apply_roles", {"profile": "123:Admin"})
    assert res.ok
    assert "/state/req/profile" in ran[0] and "123:Admin" in ran[0]


def test_start_login_parses_url_and_code_and_stores_them():
    calls = {}

    def detached(container, command, tag, script, markers, timeout):
        calls.update(container=container, command=command, tag=tag, markers=markers)
        return (
            "Please open the following URL in your browser:\r\n"
            "https://device.sso.us-east-1.amazonaws.com/?user_code=WXYZ-1234\r\n"
        )

    state: dict = {}
    res = AwsSsoExtension().handle_action(_ctx(lambda s: "", state=state, detached=detached), "start_login", {})
    assert res.ok
    assert state["login"]["code"] == "WXYZ-1234"
    assert state["login"]["url"].startswith("https://device.sso.us-east-1.amazonaws.com/")
    assert res.card.links[0].url == state["login"]["url"]
    assert calls["tag"] == "awssso_login" and calls["container"] == "aws-sso"
    script = calls["command"][-1]
    assert "login --url-action print" in script and "touch /state/login_ok" in script


def test_start_login_without_url_is_a_user_error():
    ext = AwsSsoExtension()
    ctx = _ctx(lambda s: "", detached=lambda *a: "FATAL boom\n")
    with pytest.raises(base.ExtensionError):
        ext.handle_action(ctx, "start_login", {})


def test_start_login_ignores_foreign_urls_in_log():
    ext = AwsSsoExtension()
    ctx = _ctx(lambda s: "", detached=lambda *a: "see https://evil.test/x then https://d-1.awsapps.com/start/#/device?user_code=AAAA-BBBB")
    res = ext.handle_action(ctx, "start_login", {})
    assert res.card.links[0].url.startswith("https://d-1.awsapps.com/")


# --- supervisor + protocolo de pedidos ---------------------------------------

FAKE_AWS_SSO = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$1 $2" in
  "ecs server") exec sleep 30 ;;
  "setup ecs") exit 0 ;;
  "ecs load") [ -f "$FAKE_LOAD_FAIL" ] && exit 1; exit 0 ;;
esac
case "$1" in
  cache) exit 0 ;;
  list) [ -f "$FAKE_LIST_FAIL" ] && exit 1; cat "$FAKE_ROLES" ;;
esac
exit 0
"""


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("aws_sso_supervisor", ROOT / "extensions/aws-sso/supervisor.py")
    sup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sup)

    state, home = tmp_path / "state", tmp_path / "home"
    state.mkdir()
    fake = tmp_path / "aws-sso"
    fake.write_text(FAKE_AWS_SSO)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    cfg_src = tmp_path / "config.yaml"
    cfg_src.write_text(render_config(CFG))
    (tmp_path / "roles.csv").write_text("Profile\n111111111111:Admin\n222222222222:Dev\n")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KREWHUB_AWS_SSO_TOKEN", "tok-123")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setenv("FAKE_ROLES", str(tmp_path / "roles.csv"))
    monkeypatch.setenv("FAKE_LIST_FAIL", str(tmp_path / "list_fail"))
    monkeypatch.setenv("FAKE_LOAD_FAIL", str(tmp_path / "load_fail"))
    monkeypatch.setattr(sup, "STATE", state)
    monkeypatch.setattr(sup, "CONFIG_SRC", cfg_src)
    monkeypatch.setattr(sup, "AWS_SSO", str(fake))
    s = sup.Supervisor()
    s.setup()
    yield sup, s, state, tmp_path
    if s.server:
        s.server.kill()
        s.server.wait()


def _calls(tmp_path):
    p = tmp_path / "calls.log"
    return p.read_text().splitlines() if p.exists() else []


def _status_file(state):
    return json.loads((state / "status.json").read_text())


def _run_script(state, script):
    """Roda o script do exec da extensão num sh de verdade, com /state
    apontando pro diretório do teste."""
    subprocess.run(["sh", "-c", script.replace("/state", str(state))], check=True)
    return ""


def test_supervisor_setup_copies_config_into_aws_sso_home(sidecar):
    _, _, _, tmp = sidecar
    cfg = tmp / "home/.config/aws-sso/config.yaml"
    assert "AuthWorkflow: device_code" in cfg.read_text()


def test_supervisor_starts_server_with_bearer_before_login(sidecar):
    _, s, state, tmp = sidecar
    s.tick()
    calls = _calls(tmp)
    assert "setup ecs auth --bearer-token tok-123" in calls
    assert any(c.startswith("ecs server --bind-ip 127.0.0.1 --port 4144") for c in calls)
    st = _status_file(state)
    assert st["server"] is True and st["logged_in"] is False and st["loaded"] is False


def test_full_flow_through_extension_requests(sidecar):
    _, s, state, tmp = sidecar
    ext = AwsSsoExtension()
    ctx = _ctx(lambda script: _run_script(state, script))

    s.tick()
    (state / "login_ok").touch()  # o que o fluxo de login faz ao terminar
    s.tick()
    st = _status_file(state)
    assert st["logged_in"] and st["roles"] == 2 and st["profile"] == ""
    assert not st["loaded"]

    ext.handle_action(ctx, "apply_roles", {"profile": "222222222222:Dev"})
    s.tick()
    st = _status_file(state)
    assert st["profile"] == "222222222222:Dev" and st["loaded"] is True
    assert "ecs load --profile 222222222222:Dev --server localhost:4144" in _calls(tmp)

    ext.handle_action(ctx, "reload_creds", {})
    s.tick()
    assert any(c.endswith("--sts-refresh") for c in _calls(tmp))

    ext.handle_action(ctx, "refresh_roles", {})
    before = sum(c.startswith("list") for c in _calls(tmp))
    s.tick()
    assert sum(c.startswith("list") for c in _calls(tmp)) == before + 1
    assert not list((state / "req").iterdir())  # pedidos consumidos


def test_unknown_profile_is_rejected_by_supervisor(sidecar):
    _, s, state, tmp = sidecar
    s.tick()
    (state / "login_ok").touch()
    s.tick()
    (state / "req/profile").write_text("999:Nope")
    s.tick()
    st = _status_file(state)
    assert st["profile"] == "" and "desconhecido" in st["error"]
    assert not any(c.startswith("ecs load") for c in _calls(tmp))


def test_single_role_is_selected_automatically(sidecar):
    _, s, state, tmp = sidecar
    (tmp / "roles.csv").write_text("Profile\n111111111111:Admin\n")
    s.tick()
    (state / "login_ok").touch()
    s.tick()
    assert _status_file(state)["profile"] == "111111111111:Admin"


def test_expired_session_drops_login_and_reports_it(sidecar):
    _, s, state, tmp = sidecar
    s.tick()
    (state / "login_ok").touch()
    (tmp / "list_fail").touch()
    s.tick()
    st = _status_file(state)
    assert st["logged_in"] is False and "expirada" in st["error"]
    assert not (state / "login_ok").exists()


def test_failed_load_is_reported_without_leaking_details(sidecar):
    _, s, state, tmp = sidecar
    s.tick()
    (state / "login_ok").touch()
    s.tick()
    (tmp / "load_fail").touch()
    (state / "req/profile").write_text("111111111111:Admin")
    s.tick()
    st = _status_file(state)
    assert st["loaded"] is False and "tok-123" not in json.dumps(st)
    assert st["error"]


def test_requests_without_login_are_discarded(sidecar):
    _, s, state, _ = sidecar
    (state / "req/profile").write_text("111111111111:Admin")
    s.tick()
    assert not (state / "req/profile").exists()
    assert _status_file(state)["profile"] == ""
