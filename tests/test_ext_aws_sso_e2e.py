"""Ponta a ponta da extensão AWS SSO sem cluster: endpoint -> runtime ->
plugin (carregado pelo entry point real), com `exec` e `run_detached` do
Pod simulados por um sidecar falso que devolve o log esperado do
`aws-sso login --url-action print`."""

from __future__ import annotations

import dataclasses
import json
import time

import pytest

from app import extensions, pod_exec, store
from app.extensions import runtime
from tests.ext_demo import fake_clients, pod  # noqa: F401
from tests.ws_fake import real_exec_clients

OWNER = "dev-a@test.local"
URL = "/devs/dev-a%40test.local"
HREF = "/devs/dev-a@test.local"
CODE = "WXYZ-1234"
LOGIN_URL = f"https://device.sso.us-east-1.amazonaws.com/?user_code={CODE}"
LOGIN_LOG = (
    "Please open the following URL in your browser:\r\n"
    f"{LOGIN_URL}\r\n"
    "Waiting for authentication...\r\n"
)


class FakeSidecar:
    """Estado do sidecar visto por `exec_sh` (/state/status.json) e pelo
    driver de login (`run_detached`)."""

    def __init__(self, login_log: str = LOGIN_LOG):
        self.status: dict | None = {"server": True, "logged_in": False}
        self.login_log = login_log
        self.flows: list[pod_exec.DetachedFlow] = []
        self.scripts: list[str] = []

    def exec_sh(self, c, pod_name, namespace, script, *, container, **kw):
        self.scripts.append(script)
        if "status.json" in script and ">" not in script.split("status.json")[0][-3:]:
            return json.dumps(self.status) if self.status is not None else ""
        return ""

    def run_detached(self, c, pod_name, namespace, flow):
        self.flows.append(flow)
        return self.login_log


@pytest.fixture
def login(sign_cookie):
    def _login(client):
        client.cookies.set("krewhub_session", sign_cookie(OWNER))

    return _login


@pytest.fixture
def sidecar(monkeypatch):
    sc = FakeSidecar()
    monkeypatch.setattr(pod_exec, "exec_sh", sc.exec_sh)
    monkeypatch.setattr(pod_exec, "run_detached", sc.run_detached)
    return sc


@pytest.fixture
def aws_client(client, settings, monkeypatch, fake_clients):  # noqa: F811
    import app.main as main

    extensions.reset_for_tests()  # entry point real do pacote krewhub-ext-aws-sso
    s = dataclasses.replace(settings, extensions_enabled="aws-sso")
    monkeypatch.setattr(main, "_settings", s)
    fake_clients.core.read_namespaced_pod.side_effect = None
    fake_clients.core.read_namespaced_pod.return_value = pod(sidecars=("aws-sso",))
    with store.connect(s.db_path) as conn:
        store.upsert_extension(
            conn,
            OWNER,
            "aws-sso",
            enabled=True,
            config={"start_url": "https://d-0123456789.awsapps.com/start", "sso_region": "us-east-1"},
        )
    yield client, s
    extensions.reset_for_tests()


def _runtime_state(s):
    with store.connect(s.db_path) as conn:
        return store.get_extension(conn, OWNER, "aws-sso")["runtime_state"]


def test_entry_point_plugin_is_what_the_runtime_runs(aws_client, sidecar):
    _, s = aws_client
    (view,) = runtime.evaluate(s, OWNER)
    assert view.ext_id == "aws-sso" and view.state == "needs_action"
    assert view.conditions["sidecar.aws-sso.running"] and not view.conditions["sso.logged_in"]
    assert {a.id: a.enabled for a in view.actions} == {
        "start_login": True,
        "refresh_roles": False,
        "apply_roles": False,
        "reload_creds": False,
    }


def test_start_login_end_to_end_runs_the_detached_flow_in_the_sidecar(aws_client, sidecar):
    _, s = aws_client
    res = runtime.run_action(s, OWNER, "aws-sso", "start_login", {})
    assert res.ok

    (flow,) = sidecar.flows
    assert flow.container == "aws-sso" and flow.tag == "awssso_login"
    assert flow.done_markers == ("https://",)
    assert flow.command[:2] == ("sh", "-c")
    assert "aws-sso login --url-action print" in flow.command[-1]
    assert "touch /state/login_ok" in flow.command[-1]

    login = _runtime_state(s)["login"]
    assert login["url"] == LOGIN_URL and login["code"] == CODE
    assert abs(login["at"] - time.time()) < 60


def test_card_shows_link_and_code_after_start_login_then_flips_when_logged_in(aws_client, sidecar):
    _, s = aws_client
    runtime.run_action(s, OWNER, "aws-sso", "start_login", {})

    (view,) = runtime.evaluate(s, OWNER)
    assert view.state == "needs_action"
    assert view.card.code == CODE
    assert view.card.links[0].url == LOGIN_URL

    sidecar.status = {"server": True, "logged_in": True, "roles": 2, "role_names": ["1:Admin", "2:Dev"]}
    (view,) = runtime.evaluate(s, OWNER)
    assert view.card.links == () and view.card.code == ""
    assert {a.id for a in view.actions if a.enabled} == {"start_login", "refresh_roles", "apply_roles"}


def test_start_login_over_http_with_csrf_redirects_and_renders_the_code(aws_client, sidecar, login):
    client, s = aws_client
    login(client)
    token = runtime.make_csrf(s.session_secret, OWNER, "aws-sso", "start_login")
    r = client.post(
        f"{URL}/extensions/aws-sso/actions/start_login",
        data={"csrf": token},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == f"{HREF}/extensions/cards"

    page = client.get(f"{URL}/extensions/cards")
    assert page.status_code == 200
    assert CODE in page.text
    assert LOGIN_URL.replace("&", "&amp;") in page.text or LOGIN_URL in page.text


def test_start_login_over_http_with_bearer_returns_json(aws_client, sidecar, sign_cookie):
    client, _ = aws_client
    r = client.post(
        f"{URL}/extensions/aws-sso/actions/start_login",
        headers={"authorization": f"Bearer {sign_cookie(OWNER)}"},
    )
    assert r.status_code == 200 and r.json()["ok"] is True


def test_start_login_without_url_in_log_is_a_user_error_and_keeps_no_link(aws_client, sidecar):
    _, s = aws_client
    sidecar.login_log = "FATAL: RegisterClient: 400 Bad Request\n"
    res = runtime.run_action(s, OWNER, "aws-sso", "start_login", {})
    assert not res.ok
    state = _runtime_state(s)
    assert "login" not in state
    assert state[runtime.LAST_ACTION_KEY]["ok"] is False
    (view,) = runtime.evaluate(s, OWNER)
    assert view.card.links == () and view.card.code == ""


def test_start_login_driver_failure_does_not_leak_internal_text(aws_client, sidecar, monkeypatch):
    _, s = aws_client

    def boom(*a, **kw):
        raise RuntimeError("socket /var/run/segredo-interno")

    monkeypatch.setattr(pod_exec, "run_detached", boom)
    res = runtime.run_action(s, OWNER, "aws-sso", "start_login", {})
    assert not res.ok
    assert "segredo-interno" not in res.message


def test_start_login_is_rejected_while_the_sidecar_is_not_running(aws_client, sidecar, fake_clients):  # noqa: F811
    _, s = aws_client
    fake_clients.core.read_namespaced_pod.return_value = pod(ready=False, sidecars=("aws-sso",))
    with pytest.raises(runtime.ActionRejected) as e:
        runtime.run_action(s, OWNER, "aws-sso", "start_login", {})
    assert e.value.status_code == 409
    assert sidecar.flows == []


def test_follow_up_actions_write_request_files_through_exec(aws_client, sidecar):
    _, s = aws_client
    sidecar.status = {"server": True, "logged_in": True, "roles": 1, "role_names": ["123456789012:Admin"]}
    assert runtime.run_action(s, OWNER, "aws-sso", "refresh_roles", {}).ok
    assert runtime.run_action(s, OWNER, "aws-sso", "apply_roles", {"profile": "123456789012:Admin"}).ok
    joined = "\n".join(sidecar.scripts)
    assert "/state/req/refresh" in joined
    assert "/state/req/profile" in joined and "123456789012:Admin" in joined


def _use_real_exec(monkeypatch, fake_clients, stdout):  # noqa: F811
    """`exec_sh` REAL + `stream`/`ApiClient` reais do kubernetes; só o socket
    devolve `stdout`. É o caminho que os outros testes pulam ao trocar
    `exec_sh` inteiro."""
    fake_clients.core.connect_get_namespaced_pod_exec = real_exec_clients(
        monkeypatch, stdout
    ).core.connect_get_namespaced_pod_exec


def test_status_json_written_by_the_supervisor_survives_the_real_exec_path(
    aws_client, monkeypatch, fake_clients  # noqa: F811
):
    """Regressão (cluster real): o status.json do supervisor é JSON com
    true/false/"" e o exec devolvia a repr do Python, o parse falhava em
    silêncio e o card ficava eternamente em "faça login"."""
    _, s = aws_client
    status = {
        "ts": 1769800000.5, "server": True, "logged_in": True, "profile": "",
        "loaded": False, "roles": 402, "role_names": ["111111111111:Admin"], "error": "",
    }
    _use_real_exec(monkeypatch, fake_clients, json.dumps(status) + "\n")

    (view,) = runtime.evaluate(s, OWNER)
    assert view.conditions["sso.server"] and view.conditions["sso.logged_in"]
    assert not view.conditions["sso.creds_loaded"]
    assert view.card.summary == "Login feito. Escolha o papel que o ambiente deve assumir."
    assert ("Papéis disponíveis", "402") in view.card.rows


def test_ready_status_through_the_real_exec_path(aws_client, monkeypatch, fake_clients):  # noqa: F811
    _, s = aws_client
    status = {"server": True, "logged_in": True, "profile": "1:Admin", "loaded": True, "error": ""}
    _use_real_exec(monkeypatch, fake_clients, json.dumps(status))
    (view,) = runtime.evaluate(s, OWNER)
    assert view.state == "ready"


def test_unreadable_status_json_is_logged_not_silent(aws_client, monkeypatch, fake_clients, caplog):  # noqa: F811
    _, s = aws_client
    _use_real_exec(monkeypatch, fake_clients, "{'server': True}")
    with caplog.at_level("WARNING", logger="krewhub.ext.aws-sso"):
        (view,) = runtime.evaluate(s, OWNER)
    assert view.state == "needs_action" and not view.conditions["sso.logged_in"]
    assert "status.json ilegível" in caplog.text


def test_cards_refresh_by_themselves_after_start_login_until_the_dev_authorizes(
    aws_client, sidecar, login
):
    """Regressão (cluster real): depois de `start_login` o card diz "a
    página atualiza sozinha", mas o estado é `needs_action` e nada
    recarregava -- o dev ficava olhando o código para sempre."""
    client, s = aws_client
    login(client)
    runtime.run_action(s, OWNER, "aws-sso", "start_login", {})

    waiting = client.get(f"{URL}/extensions/cards")
    assert CODE in waiting.text and 'http-equiv="refresh"' in waiting.text

    sidecar.status = {
        "server": True, "logged_in": True, "roles": 2, "role_names": ["1:Admin", "2:Dev"],
    }
    choosing = client.get(f"{URL}/extensions/cards")
    assert "Escolha o papel" in choosing.text
    assert 'http-equiv="refresh"' not in choosing.text  # o dev está digitando o perfil


@pytest.mark.parametrize(
    "status,polling",
    [
        ({"server": True, "logged_in": False}, False),  # nada pedido ainda
        ({"server": True, "logged_in": True, "roles": 1, "role_names": ["1:A"]}, True),  # auto-seleção
        ({"server": True, "logged_in": True, "roles": 3}, False),  # dev escolhe
        ({"server": True, "logged_in": True, "profile": "1:A", "loaded": False}, True),
        ({"server": True, "logged_in": True, "profile": "1:A", "loaded": False, "error": "boom"}, False),
        ({"server": True, "logged_in": True, "profile": "1:A", "loaded": True}, False),
    ],
)
def test_aws_sso_card_polls_only_while_waiting_on_something(aws_client, sidecar, status, polling):
    _, s = aws_client
    sidecar.status = status
    (view,) = runtime.evaluate(s, OWNER)
    assert view.card.polling is polling


def test_card_lists_account_names_and_apply_roles_takes_the_account_id(aws_client, sidecar, login):
    """Regressão (cluster real): contas com parênteses/acentos no nome davam
    "Perfil inválido". O valor enviado é `<id>:<papel>`; o nome é rótulo."""
    client, s = aws_client
    login(client)
    profile = "000123456789:AWS-DevSecOps"
    sidecar.status = {
        "server": True, "logged_in": True, "roles": 2,
        "role_names": [profile, "111111111111:AWS-CloudAdmin"],
        "role_labels": {profile: "EdSaraiva(AdministradorAWS-AMAZON)", "111111111111:AWS-CloudAdmin": "RedaçãoNota1000"},
    }
    page = client.get(f"{URL}/extensions/cards").text
    assert "EdSaraiva(AdministradorAWS-AMAZON)" in page and "RedaçãoNota1000" in page

    assert runtime.run_action(s, OWNER, "aws-sso", "apply_roles", {"profile": profile}).ok
    assert profile in " ".join(sidecar.scripts)
    assert not runtime.run_action(
        s, OWNER, "aws-sso", "apply_roles", {"profile": "EdSaraiva(AdministradorAWS-AMAZON):AWS-DevSecOps"}
    ).ok
