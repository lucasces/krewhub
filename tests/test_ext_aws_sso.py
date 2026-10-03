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
from app.extensions.contributions import ContributionError, collect_files
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


def test_render_config_pins_profile_to_account_id_and_role():
    """O default do aws-sso-cli usa o NOME da conta no perfil (parênteses,
    acentos...). O id zero-preenchido tem alfabeto fechado."""
    assert 'ProfileFormat: "{{ .AccountIdPad }}:{{ .RoleName }}"\n' in render_config(CFG)


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
    assert set(mounts) == {"/state", "/tmp", "/etc/aws-sso", "/profiles"}
    assert mounts["/etc/aws-sso"]["readOnly"] is True
    assert "readOnly" not in mounts["/profiles"]
    # o token SSO vive no emptyDir privado: o kirocrew não o monta
    assert "aws-sso-state" not in {m["name"] for m in main["volumeMounts"]}
    vols = {v["name"]: v for v in spec["volumes"]}
    assert vols["aws-sso-state"] == {"name": "aws-sso-state", "emptyDir": {}}
    assert vols["aws-sso-profiles"] == {"name": "aws-sso-profiles", "emptyDir": {}}
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


def test_pod_shares_the_managed_profiles_file_with_kirocrew_read_only(monkeypatch):
    pod = _pod(monkeypatch)
    main, side = pod["spec"]["containers"]
    mounts = {m["mountPath"]: m for m in main["volumeMounts"]}
    assert mounts["/etc/krewhub/aws-sso"] == {"name": "aws-sso-profiles", "mountPath": "/etc/krewhub/aws-sso", "readOnly": True}
    env = {e["name"]: e.get("value") for e in main["env"]}
    assert env["AWS_CONFIG_FILE"] == "/etc/krewhub/aws-sso/config"
    # o supervisor escreve o `credential_process` apontando pro helper que o initContainer copia
    side_env = {e["name"]: e.get("value") for e in side["env"]}
    assert side_env["AWS_SSO_HELPER"] == "/opt/krewhub-ext/aws-sso/bin/krewhub-aws-sso-creds"


def test_pod_copies_the_aws_cli_into_kirocrew_through_an_init_container(monkeypatch):
    pod = _pod(monkeypatch, "registry.test/krewhub-ext-aws-sso:9")
    spec = pod["spec"]
    (init,) = spec["initContainers"]
    # mesma imagem do sidecar: um pull só, uma versão só pra fixar
    assert init["name"] == "aws-sso-tools"
    assert init["image"] == "registry.test/krewhub-ext-aws-sso:9"
    assert init["command"][:2] == ["sh", "-c"]
    assert "/opt/krewhub-tools/. /tools/" in init["command"][2]
    assert init["securityContext"]["readOnlyRootFilesystem"] is True
    main = spec["containers"][0]
    mount = next(m for m in main["volumeMounts"] if m["name"] == "aws-sso-tools")
    assert mount == {"name": "aws-sso-tools", "mountPath": "/opt/krewhub-ext/aws-sso", "readOnly": True}
    path = next(e["value"] for e in main["env"] if e["name"] == "PATH")
    assert path.split(":")[0] == "/opt/krewhub-ext/aws-sso/bin"


def test_pod_mounts_the_agent_skill_from_the_tools_volume_where_kiro_discovers_it(monkeypatch):
    pod = _pod(monkeypatch)
    main = pod["spec"]["containers"][0]
    mount = next(m for m in main["volumeMounts"] if m["mountPath"] == "/home/kirocrew/.kiro/skills/aws-sso")
    assert mount == {
        "name": "aws-sso-tools",
        "mountPath": "/home/kirocrew/.kiro/skills/aws-sso",
        "subPath": "skills/aws-sso",
        "readOnly": True,
    }
    # a skill vem da imagem: nada dela no ConfigMap de files nem no hash por conteúdo
    assert "aws-sso-skill" not in {v["name"] for v in pod["spec"]["volumes"]}
    contrib = AwsSsoExtension().pod_contribution(base.BuildContext("d@t", "dev-test-local", "ns", CFG, None))
    assert contrib.skills == {}
    assert set(collect_files([("aws-sso", contrib)])) == {"aws-sso.config.yaml"}


SKILL_FILE = Path(__file__).resolve().parent.parent / "extensions" / "aws-sso" / "skills" / "aws-sso" / "SKILL.md"


def test_skill_has_kiro_frontmatter_and_the_guidance_the_agent_needs():
    from krewhub_ext_aws_sso import SKILL_NAME

    SKILL_MD = SKILL_FILE.read_text()
    head, _, body = SKILL_MD[4:].partition("\n---\n")
    fields = dict(line.split(": ", 1) for line in head.splitlines())
    assert SKILL_MD.startswith("---\n")
    assert fields["name"] == SKILL_NAME == "aws-sso"
    assert len(fields["description"]) > 80
    for needle in (
        "aws sts get-caller-identity",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "/opt/krewhub-ext/aws-sso/bin/aws",
        "aws configure",
        "aws sso login",
        "aws configure list-profiles",
        "--profile",
        "AWS_PROFILE",
        "AWS_CONFIG_FILE",
        "default",
        "boto3",
        "KrewHub",
    ):
        assert needle in body, needle
    # os papéis ativos são perfis nomeados, mas o agente nunca faz login nem configura nada
    assert "There are **no profiles**" not in body and "Do not pass `--profile`" not in body
    # nada de credencial de exemplo no texto
    assert "AKIA" not in SKILL_MD and "ASIA" not in SKILL_MD


def test_dockerfile_ships_the_pinned_aws_cli_where_the_init_container_copies_it_from():
    import re

    from krewhub_ext_aws_sso import TOOLS_SOURCE_DIR

    dockerfile = (Path(__file__).resolve().parent.parent / "extensions" / "aws-sso" / "Dockerfile").read_text()
    assert f"COPY --from=awscli /out {TOOLS_SOURCE_DIR}" in dockerfile
    assert f"COPY skills {TOOLS_SOURCE_DIR}/skills" in dockerfile
    assert "ln -s ../aws-cli/aws /out/bin/aws" in dockerfile
    assert f"COPY credential_process.py {TOOLS_SOURCE_DIR}/bin/krewhub-aws-sso-creds" in dockerfile
    for arch in ("AMD64", "ARM64"):
        assert re.search(rf"ARG AWSCLI_SHA256_{arch}=[0-9a-f]{{64}}\b", dockerfile)
    assert "sha256sum -c" in dockerfile


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


def test_status_logged_in_without_role_offers_every_role_as_a_checkbox_option():
    st = _status({"server": True, "logged_in": True, "roles": 2, "role_names": ["111111111111:Admin", "222222222222:Dev"], "profiles": []})
    assert st.state == "needs_action"
    assert "Marque" in st.card.summary
    opts = st.choices["apply_roles.profiles"]
    assert [(c.value, c.checked) for c in opts] == [("111111111111:Admin", False), ("222222222222:Dev", False)]


def test_status_ready_with_one_role_shows_no_default_marker():
    st = _status({"server": True, "logged_in": True, "profile": "111111111111:Admin", "profiles": ["111111111111:Admin"],
                  "loaded_profiles": ["111111111111:Admin"], "loaded": True})
    assert st.state == "ready"
    assert ("Papel", "111111111111:Admin") in st.card.rows
    assert st.conditions["sso.role_selected"] and st.conditions["sso.creds_loaded"]


def test_status_lists_all_active_roles_marks_the_default_and_the_ones_without_credentials():
    active = ["111111111111:Admin", "222222222222:Dev", "333333333333:Ops"]
    st = _status({
        "server": True, "logged_in": True, "profile": active[0], "profiles": active,
        "loaded_profiles": active[:2], "loaded": False, "roles": 4,
        "role_names": [*active, "444444444444:Audit"],
        "role_labels": {active[0]: "Prod"},
        "error": "Não foi possível carregar as credenciais de um ou mais papéis.",
    })
    assert ("Papel", "111111111111:Admin (padrão)") in st.card.rows
    assert ("Papel", "222222222222:Dev") in st.card.rows
    assert ("Papel", "333333333333:Ops (sem credenciais)") in st.card.rows
    assert ("Conta", "Prod") in st.card.rows
    assert st.state != "ready"
    opts = st.choices["apply_roles.profiles"]
    assert [(c.value, c.checked) for c in opts] == [(r, True) for r in active] + [("444444444444:Audit", False)]


def test_ready_card_with_several_roles_explains_how_to_pick_a_non_default_one():
    active = ["111111111111:Admin", "222222222222:Dev"]
    st = _status({"server": True, "logged_in": True, "profile": active[0], "profiles": active,
                  "loaded_profiles": active, "loaded": True})
    assert st.state == "ready"
    assert any("--profile" in m and active[0] in m for m in st.card.messages)


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
        ext.handle_action(ctx, "apply_roles", {"profiles": ("123456789012:Admin", "x; rm -rf /")})
    assert ran == []


def test_apply_roles_writes_the_desired_set_one_per_line_in_order():
    ran: list[str] = []
    ext = AwsSsoExtension()
    res = ext.handle_action(
        _ctx(lambda s: ran.append(s) or ""), "apply_roles",
        {"profiles": ("222222222222:Dev", "123456789012:Admin", "222222222222:Dev")},
    )
    assert res.ok
    assert "/state/req/profiles" in ran[0]
    assert "'222222222222:Dev\n123456789012:Admin'" in ran[0]


def test_apply_roles_caps_the_number_of_active_roles():
    ext = AwsSsoExtension()
    ok = tuple(f"{i:012d}:R" for i in range(10))
    assert ext.handle_action(_ctx(lambda s: ""), "apply_roles", {"profiles": ok}).ok
    with pytest.raises(base.ExtensionError, match="no máximo 10"):
        ext.handle_action(_ctx(lambda s: ""), "apply_roles", {"profiles": (*ok, "999999999999:R")})


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
    monkeypatch.setattr(sup, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(sup, "HELPER", "/opt/krewhub-ext/aws-sso/bin/krewhub-aws-sso-creds")
    monkeypatch.setattr(sup, "CONFIG_SRC", cfg_src)
    monkeypatch.setattr(sup, "AWS_SSO", str(fake))
    monkeypatch.setattr(sup, "delete_slot", lambda profile: _record_delete(tmp_path, profile))
    s = sup.Supervisor()
    s.setup()
    yield sup, s, state, tmp_path
    if s.server:
        s.server.kill()
        s.server.wait()


def _record_delete(tmp_path, profile):
    with (tmp_path / "calls.log").open("a") as f:
        f.write(f"DELETE /slot/{profile}\n")


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


def _login(state, s):
    s.tick()
    (state / "login_ok").touch()  # o que o fluxo de login faz ao terminar
    s.tick()


def _loads(tmp_path):
    return [c for c in _calls(tmp_path) if c.startswith("ecs load")]


def test_full_flow_through_extension_requests(sidecar):
    _, s, state, tmp = sidecar
    ext = AwsSsoExtension()
    ctx = _ctx(lambda script: _run_script(state, script))

    _login(state, s)
    st = _status_file(state)
    assert st["logged_in"] and st["roles"] == 2 and st["profiles"] == []
    assert not st["loaded"]

    ext.handle_action(ctx, "apply_roles", {"profiles": ("222222222222:Dev",)})
    s.tick()
    st = _status_file(state)
    assert st["profile"] == "222222222222:Dev" and st["profiles"] == ["222222222222:Dev"] and st["loaded"] is True
    assert "ecs load --profile 222222222222:Dev --server localhost:4144" in _calls(tmp)

    ext.handle_action(ctx, "reload_creds", {})
    s.tick()
    assert any(c.endswith("--sts-refresh") for c in _calls(tmp))

    ext.handle_action(ctx, "refresh_roles", {})
    before = sum(c.startswith("list") for c in _calls(tmp))
    s.tick()
    assert sum(c.startswith("list") for c in _calls(tmp)) == before + 1
    assert not list((state / "req").iterdir())  # pedidos consumidos


def test_first_role_is_the_default_and_the_others_are_slotted(sidecar):
    _, s, state, tmp = sidecar
    _login(state, s)
    (state / "req/profiles").write_text("222222222222:Dev\n111111111111:Admin")
    s.tick()
    assert _loads(tmp) == [
        "ecs load --profile 222222222222:Dev --server localhost:4144",
        "ecs load --profile 111111111111:Admin --server localhost:4144 --slotted",
    ]
    st = _status_file(state)
    assert st["profile"] == "222222222222:Dev"
    assert st["profiles"] == ["222222222222:Dev", "111111111111:Admin"]
    assert st["loaded_profiles"] == st["profiles"] and st["loaded"] is True


def test_profiles_file_has_one_credential_process_profile_per_active_role(sidecar):
    _, s, state, tmp = sidecar
    assert "[profile" not in (tmp / "profiles/config").read_text()
    _login(state, s)
    (state / "req/profiles").write_text("222222222222:Dev\n111111111111:Admin")
    s.tick()
    helper = "/opt/krewhub-ext/aws-sso/bin/krewhub-aws-sso-creds"
    assert (tmp / "profiles/config").read_text().splitlines()[1:] == [
        "",
        "[profile 222222222222:Dev]",
        f"credential_process = python3 {helper} 222222222222:Dev --default",
        "",
        "[profile 111111111111:Admin]",
        f"credential_process = python3 {helper} 111111111111:Admin",
    ]
    assert not (tmp / "profiles/config.tmp").exists()


def test_selection_is_a_desired_set_that_unloads_dropped_slots_and_promotes_the_default(sidecar):
    _, s, state, tmp = sidecar
    (tmp / "roles.csv").write_text("Profile\n111111111111:Admin\n222222222222:Dev\n333333333333:Ops\n")
    _login(state, s)
    (state / "req/profiles").write_text("111111111111:Admin\n222222222222:Dev\n333333333333:Ops")
    s.tick()
    calls_before = len(_calls(tmp))

    # tira o padrão e um slot: Dev vira o padrão, Ops sai
    (state / "req/profiles").write_text("222222222222:Dev")
    s.tick()
    new = _calls(tmp)[calls_before:]
    assert "DELETE /slot/333333333333:Ops" in new
    # Dev sai do slot antes de virar padrão; o padrão antigo (Admin) nunca é apagado, só substituído
    assert "DELETE /slot/222222222222:Dev" in new
    assert "DELETE /slot/111111111111:Admin" not in new
    assert new.index("DELETE /slot/222222222222:Dev") < new.index("ecs load --profile 222222222222:Dev --server localhost:4144")
    st = _status_file(state)
    assert st["profiles"] == ["222222222222:Dev"] and st["loaded_profiles"] == ["222222222222:Dev"]
    assert "[profile 111111111111:Admin]" not in (tmp / "profiles/config").read_text()


def test_empty_selection_is_rejected_and_keeps_the_active_roles(sidecar):
    _, s, state, tmp = sidecar
    _login(state, s)
    (state / "req/profiles").write_text("111111111111:Admin")
    s.tick()
    (state / "req/profiles").write_text("\n")
    s.tick()
    st = _status_file(state)
    assert st["profiles"] == ["111111111111:Admin"] and "ao menos um" in st["error"]


def test_unchanged_selection_is_not_reloaded_on_every_tick(sidecar):
    _, s, state, tmp = sidecar
    _login(state, s)
    (state / "req/profiles").write_text("111111111111:Admin\n222222222222:Dev")
    s.tick()
    n = len(_loads(tmp))
    s.tick()
    s.tick()
    assert len(_loads(tmp)) == n


def test_reload_request_refreshes_every_active_role(sidecar):
    _, s, state, tmp = sidecar
    _login(state, s)
    (state / "req/profiles").write_text("111111111111:Admin\n222222222222:Dev")
    s.tick()
    (state / "req/reload").write_text("1")
    s.tick()
    refreshed = [c for c in _loads(tmp) if c.endswith("--sts-refresh")]
    assert len(refreshed) == 2
    assert any("222222222222:Dev" in c and "--slotted" in c for c in refreshed)


def test_selection_survives_a_supervisor_restart_and_a_new_login(sidecar, monkeypatch):
    sup, s, state, tmp = sidecar
    _login(state, s)
    (state / "req/profiles").write_text("222222222222:Dev\n111111111111:Admin")
    s.tick()
    (state / "login_ok").unlink()
    s.tick()
    st = _status_file(state)
    assert st["profiles"] == ["222222222222:Dev", "111111111111:Admin"] and st["loaded"] is False

    s2 = sup.Supervisor()
    s2.setup()
    s2.server = s.server
    assert s2.selected == ["222222222222:Dev", "111111111111:Admin"]
    assert "[profile 111111111111:Admin]" in (tmp / "profiles/config").read_text()
    (state / "login_ok").touch()
    n = len(_loads(tmp))
    s2.tick()
    assert len(_loads(tmp)) == n + 2 and _status_file(state)["loaded"] is True


def test_more_roles_than_the_cap_is_rejected_without_touching_the_selection(sidecar):
    sup, s, state, tmp = sidecar
    names = [f"{i:012d}:R" for i in range(sup.MAX_ROLES + 1)]
    (tmp / "roles.csv").write_text("Profile\n" + "\n".join(names) + "\n")
    _login(state, s)
    (state / "req/profiles").write_text("\n".join(names))
    s.tick()
    st = _status_file(state)
    assert st["profiles"] == [] and "No máximo" in st["error"]
    assert _loads(tmp) == []


def test_unknown_profile_is_rejected_by_supervisor(sidecar):
    _, s, state, tmp = sidecar
    _login(state, s)
    (state / "req/profiles").write_text("111111111111:Admin\n999999999999:Nope")
    s.tick()
    st = _status_file(state)
    assert st["profiles"] == [] and "desconhecido" in st["error"]
    assert not _loads(tmp)


def test_single_role_is_selected_automatically(sidecar):
    _, s, state, tmp = sidecar
    (tmp / "roles.csv").write_text("Profile\n111111111111:Admin\n")
    _login(state, s)
    assert _status_file(state)["profiles"] == ["111111111111:Admin"]
    assert "[profile 111111111111:Admin]" in (tmp / "profiles/config").read_text()


def test_expired_session_drops_login_and_reports_it(sidecar):
    _, s, state, tmp = sidecar
    s.tick()
    (state / "login_ok").touch()
    (tmp / "list_fail").touch()
    s.tick()
    st = _status_file(state)
    assert st["logged_in"] is False and "expirada" in st["error"]
    assert not (state / "login_ok").exists()


def test_failed_load_is_reported_without_leaking_details_and_retried_later(sidecar, monkeypatch):
    sup, s, state, tmp = sidecar
    _login(state, s)
    (tmp / "load_fail").touch()
    (state / "req/profiles").write_text("111111111111:Admin\n222222222222:Dev")
    s.tick()
    st = _status_file(state)
    assert st["loaded"] is False and st["loaded_profiles"] == [] and "tok-123" not in json.dumps(st)
    assert st["error"]

    n = len(_loads(tmp))
    s.tick()  # ainda dentro da janela de espera: nada de martelar o servidor
    assert len(_loads(tmp)) == n

    (tmp / "load_fail").unlink()
    monkeypatch.setattr(sup, "RETRY_SECONDS", 0)
    s.tick()
    st = _status_file(state)
    assert st["loaded"] is True and st["error"] == ""


def test_one_failing_role_does_not_block_the_others(sidecar):
    _, s, state, tmp = sidecar
    _login(state, s)
    fake = tmp / "aws-sso"
    fake.write_text(fake.read_text().replace(
        '"ecs load") [ -f "$FAKE_LOAD_FAIL" ] && exit 1; exit 0 ;;',
        '"ecs load") case "$*" in *111111111111:Admin*) exit 1 ;; esac; exit 0 ;;',
    ))
    (state / "req/profiles").write_text("111111111111:Admin\n222222222222:Dev")
    s.tick()
    st = _status_file(state)
    assert st["loaded_profiles"] == ["222222222222:Dev"] and st["loaded"] is False and st["error"]


def test_requests_without_login_are_discarded(sidecar):
    _, s, state, _ = sidecar
    (state / "req/profiles").write_text("111111111111:Admin")
    s.tick()
    assert not (state / "req/profiles").exists()
    assert _status_file(state)["profiles"] == []


# --- perfil = <id da conta>:<papel>; nome da conta é só rótulo -----------------

TRICKY_NAMES = [
    "EdSaraiva(AdministradorAWS-AMAZON)",
    "RedaçãoNota1000",
    'Produção (Cliente, A) "x"',
    "Conta; rm -rf /",
]


@pytest.mark.parametrize("profile", ["000123456789:AWS-DevSecOps", "111111111111:Admin_Role", "222222222222:a+b=c,d.e@f-g"])
def test_apply_roles_accepts_account_id_and_role(profile):
    ran: list[str] = []
    res = AwsSsoExtension().handle_action(_ctx(lambda s: ran.append(s) or ""), "apply_roles", {"profiles": (profile,)})
    assert res.ok and profile in ran[0]


@pytest.mark.parametrize(
    "profile",
    [
        "EdSaraiva(AdministradorAWS-AMAZON):AWS-DevSecOps",  # formato antigo (nome da conta)
        "123:Admin",  # id sem zero-preenchimento
        "1234567890123:Admin",
        "111111111111:",
        "111111111111:Ad min",
        "111111111111:Admin$(id)",
        "111111111111:Admin\n222222222222:Dev",
        "111111111111/Admin",
        "",
    ],
)
def test_apply_roles_rejects_anything_but_account_id_and_role(profile):
    ran: list[str] = []
    with pytest.raises(base.ExtensionError):
        AwsSsoExtension().handle_action(_ctx(lambda s: ran.append(s) or ""), "apply_roles", {"profiles": (profile,)})
    assert ran == []


def test_options_show_readable_account_names_next_to_the_profile_value():
    st = _status(
        {
            "server": True, "logged_in": True, "roles": 2,
            "role_names": ["000123456789:AWS-DevSecOps", "222222222222:Dev"],
            "role_labels": {"000123456789:AWS-DevSecOps": "EdSaraiva(AdministradorAWS-AMAZON)", "222222222222:Dev": ""},
        }
    )
    first, second = st.choices["apply_roles.profiles"]
    assert first.value == "000123456789:AWS-DevSecOps"
    assert first.label == "000123456789:AWS-DevSecOps (EdSaraiva(AdministradorAWS-AMAZON))"
    assert (second.value, second.label) == ("222222222222:Dev", "222222222222:Dev")


def test_card_shows_the_account_name_of_the_active_role():
    st = _status(
        {
            "server": True, "logged_in": True, "profiles": ["000123456789:Admin"], "loaded": True,
            "loaded_profiles": ["000123456789:Admin"], "role_labels": {"000123456789:Admin": "RedaçãoNota1000"},
        }
    )
    assert ("Papel", "000123456789:Admin") in st.card.rows
    assert ("Conta", "RedaçãoNota1000") in st.card.rows


def test_card_html_escapes_account_names():
    from app.extensions import ui

    st = _status(
        {
            "server": True, "logged_in": True, "profiles": ["111111111111:Admin"], "loaded": True,
            "loaded_profiles": ["111111111111:Admin"],
            "role_labels": {"111111111111:Admin": "<script>alert(1)</script>"},
        }
    )
    html = ui.render_cards_document(
        "o", [ui.CardView("aws-sso", "AWS SSO", st.state, st.card, (), "")], lambda e, a: ""
    )
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_supervisor_publishes_account_names_as_labels_only(sidecar):
    _, s, state, tmp = sidecar
    rows = [
        ("000123456789:AWS-DevSecOps", "EdSaraiva(AdministradorAWS-AMAZON)"),
        ("111111111111:AWS-CloudAdmin", "RedaçãoNota1000"),
        ("222222222222:Dev", 'Produção (Cliente, A) "x"'),
    ]
    import csv as _csv
    import io as _io

    buf = _io.StringIO()
    w = _csv.writer(buf)
    for r in rows:
        w.writerow(r)
    (tmp / "roles.csv").write_text("Profile,AccountName\n" + buf.getvalue(), encoding="utf-8")
    s.tick()
    (state / "login_ok").touch()
    s.tick()
    st = _status_file(state)
    assert st["role_names"] == [p for p, _ in rows]
    assert st["role_labels"] == dict(rows)
    assert "list --csv Profile AccountName" in _calls(tmp)

    (state / "req/profiles").write_text("000123456789:AWS-DevSecOps")
    s.tick()
    st = _status_file(state)
    assert st["profile"] == "000123456789:AWS-DevSecOps" and st["loaded"] is True
    assert st["role_labels"]["000123456789:AWS-DevSecOps"] == "EdSaraiva(AdministradorAWS-AMAZON)"
    assert "ecs load --profile 000123456789:AWS-DevSecOps --server localhost:4144" in _calls(tmp)


def test_supervisor_rejects_old_style_profile_with_account_name(sidecar):
    _, s, state, tmp = sidecar
    (tmp / "roles.csv").write_text("Profile,AccountName\n111111111111:Admin,Plain\n222222222222:Dev,Other\n")
    s.tick()
    (state / "login_ok").touch()
    s.tick()
    (state / "req/profiles").write_text("Plain:Admin")
    s.tick()
    assert _status_file(state)["profiles"] == []
    assert not any(c.startswith("ecs load") for c in _calls(tmp))


def test_supervisor_tolerates_list_output_without_account_name_column(sidecar):
    _, s, state, _ = sidecar
    s.tick()
    (state / "login_ok").touch()
    s.tick()
    st = _status_file(state)
    assert st["roles"] == 2 and st["role_labels"] == {p: "" for p in st["role_names"]}
