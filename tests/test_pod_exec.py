"""app/pod_exec.py -- exec genérico (container parametrizado) e o driver
destacado pty+FIFO. `kubernetes.stream.stream` mockado."""

from __future__ import annotations

import base64
from unittest import mock

import pytest

from app import k8s_manager, pod_exec
from app.k8s_templates import OWNER_LABEL_KEY
from tests.ws_fake import FakeWS, real_exec_clients, wsfy


@pytest.fixture
def clients():
    return k8s_manager.Clients(core=mock.MagicMock(), net=mock.MagicMock())


def _pod(name, phase="Running"):
    p = mock.Mock()
    p.metadata.name = name
    p.status.phase = phase
    return p


def test_find_dev_pod_filters_by_slug_and_running(clients):
    clients.core.list_namespaced_pod.return_value = mock.Mock(
        items=[_pod("kirocrew-a-pending", "Pending"), _pod("kirocrew-a-1")]
    )
    assert pod_exec.find_dev_pod(clients, "ns", "a") == "kirocrew-a-1"
    assert clients.core.list_namespaced_pod.call_args.kwargs["label_selector"] == (
        f"app=kirocrew,{OWNER_LABEL_KEY}=a"
    )


def test_find_dev_pod_raises_the_callers_error_class(clients):
    clients.core.list_namespaced_pod.return_value = mock.Mock(items=[])

    class MyError(RuntimeError):
        pass

    with pytest.raises(MyError, match="nenhum pod"):
        pod_exec.find_dev_pod(clients, "ns", "a", error_cls=MyError)


def test_exec_sh_targets_the_requested_container(monkeypatch, clients):
    seen = {}

    def fake(_fn, pod, ns, **kw):
        seen.update(pod=pod, ns=ns, **kw)
        return "out"

    monkeypatch.setattr(pod_exec, "stream", wsfy(fake))
    assert pod_exec.exec_sh(clients, "p", "ns", "echo hi", container="aws-sso") == "out"
    assert seen["container"] == "aws-sso"
    assert seen["command"] == ["sh", "-c", "echo hi"]
    assert seen["tty"] is False


def test_exec_defaults_to_the_main_container(monkeypatch, clients):
    seen = {}
    monkeypatch.setattr(pod_exec, "stream", wsfy(lambda _f, _p, _n, **kw: seen.update(kw) or ""))
    pod_exec.exec_command(clients, "p", "ns", ["true"])
    assert seen["container"] == "kirocrew"


def test_flow_rejects_unsafe_tag():
    with pytest.raises(ValueError):
        pod_exec.DetachedFlow(container="c", command=("x",), tag="../evil")


def test_run_detached_drives_the_script_in_the_right_container(monkeypatch, clients):
    monkeypatch.setattr(pod_exec.time, "sleep", lambda _s: None)
    log = "Prompt one:\nPrompt two:\nDONE\n"
    calls = []

    def fake(_fn, _pod, _ns, **kw):
        calls.append(kw)
        script = kw["command"][-1]
        return log if script.startswith("cat ") else ""

    monkeypatch.setattr(pod_exec, "stream", wsfy(fake))
    flow = pod_exec.DetachedFlow(
        container="side",
        command=("tool", "login", "--flag", "a b"),
        tag="t1",
        script=(("Prompt one", "\r"), ("Prompt two", "y\n")),
        done_markers=("DONE",),
    )
    assert pod_exec.run_detached(clients, "p", "ns", flow) == log

    assert {kw["container"] for kw in calls} == {"side"}
    scripts = [kw["command"][-1] for kw in calls]
    launch = next(s for s in scripts if "setsid" in s)
    assert "/tmp/t1_driver.py /tmp/t1_fifo tool login --flag 'a b'" in launch
    assert "> /tmp/t1_log" in launch
    sends = [s for s in scripts if "tee /tmp/t1_fifo" in s]
    payloads = [base64.b64decode(s.split()[1]).decode() for s in sends]
    assert payloads == ["\r", "y\n"]


def test_run_detached_times_out_with_log_in_message(monkeypatch, clients):
    monkeypatch.setattr(pod_exec.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        pod_exec, "stream", wsfy(lambda _f, _p, _n, **kw: "partial" if kw["command"][-1].startswith("cat") else "")
    )
    flow = pod_exec.DetachedFlow(
        container="c", command=("x",), tag="t2", done_markers=("NEVER",), stage_timeout=0
    )
    with pytest.raises(pod_exec.PodExecError, match="NEVER"):
        pod_exec.run_detached(clients, "p", "ns", flow)


# --- a saída do exec volta byte a byte (nada de repr do Python) -------------

JSON_STDOUTS = [
    '{"ts": 1.5, "server": true, "n": null, "arr": [1, 2, "x"]}\n',
    '{"logged_in": false, "role_names": ["1:Admin", "2:Dev"], "error": ""}',
    '{"msg": "ele disse \\"oi\\" e \'tchau\'", "ok": true}\n',
    '[true, false, null, "a", 1]\n',
    "true\n",
    "null",
    "123\n",
    '"texto"\n',
]


@pytest.mark.parametrize("stdout", JSON_STDOUTS)
def test_exec_sh_returns_json_looking_output_exactly(monkeypatch, stdout):
    """Regressão: com o retorno pré-carregado do `stream`, o `ApiClient`
    do kubernetes passava a saída por `json.loads` + `str()`, devolvendo
    `{'server': True, 'n': None}`. Aqui o `stream`/`ApiClient` são os
    reais; só o socket é falso."""
    c = real_exec_clients(monkeypatch, stdout)
    assert pod_exec.exec_sh(c, "p", "ns", "cat /state/status.json") == stdout


@pytest.mark.parametrize("stdout", JSON_STDOUTS)
def test_exec_command_returns_json_looking_output_exactly(monkeypatch, stdout):
    c = real_exec_clients(monkeypatch, stdout)
    assert pod_exec.exec_command(c, "p", "ns", ["cat", "x"], container="side") == stdout


def test_the_preloaded_stream_result_is_what_corrupts_json(monkeypatch):
    """Documenta (e trava) a causa raiz: o caminho `_preload_content=True`
    do cliente kubernetes DEVE continuar corrompendo; se um dia deixar de
    fazê-lo, a defesa em `exec_command` pode ser reavaliada."""
    from kubernetes.stream import stream

    c = real_exec_clients(monkeypatch, '{"server": true, "n": null}\n')
    preloaded = stream(
        c.core.connect_get_namespaced_pod_exec,
        "p", "ns", container="k", command=["x"], stderr=True, stdin=False, stdout=True, tty=False,
    )
    assert preloaded == "{'server': True, 'n': None}"


def test_exec_command_closes_the_socket_and_applies_the_timeout(monkeypatch, clients):
    ws = FakeWS("ok")
    monkeypatch.setattr(pod_exec, "stream", lambda *_a, **_kw: ws)
    assert pod_exec.exec_command(clients, "p", "ns", ["true"], timeout=7) == "ok"
    assert ws.run_timeout == 7 and ws.closed


def test_exec_command_closes_the_socket_when_reading_fails(monkeypatch, clients):
    class Boom(FakeWS):
        def run_forever(self, timeout=None):
            raise RuntimeError("socket caiu")

    ws = Boom("")
    monkeypatch.setattr(pod_exec, "stream", lambda *_a, **_kw: ws)
    with pytest.raises(RuntimeError):
        pod_exec.exec_command(clients, "p", "ns", ["true"])
    assert ws.closed


def test_exec_command_without_container_omits_the_kwarg(monkeypatch, clients):
    seen = {}
    monkeypatch.setattr(pod_exec, "stream", lambda _f, _p, _n, **kw: seen.update(kw) or FakeWS(""))
    pod_exec.exec_command(clients, "p", "ns", ["true"], container=None)
    assert "container" not in seen and seen["_preload_content"] is False
