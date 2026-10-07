"""Dublês do `kubernetes.stream` pros testes de exec.

`FakeWS`/`wsfy` imitam o `WSClient` que `stream(..., _preload_content=False)`
devolve. `real_exec_clients` vai além: usa o `stream` e o `ApiClient` REAIS
do cliente kubernetes e só troca o socket (`WSClient`) por um que entrega
um stdout fixo -- então a desserialização que corrompia JSON em repr
Python acontece de verdade, sem cluster."""

from __future__ import annotations

from kubernetes import client as k8s_client
from kubernetes.stream import ws_client

from app import k8s_manager


class FakeWS:
    def __init__(self, text: str):
        self._text = text
        self.closed = False
        self.run_timeout = None

    def run_forever(self, timeout=None):
        self.run_timeout = timeout

    def read_all(self):
        return self._text

    def close(self, **kwargs):
        self.closed = True


def wsfy(fn):
    """Adapta um fake `stream` que devolve `str` pro contrato novo (WSClient)."""

    def wrapper(*args, **kwargs):
        kwargs.pop("_preload_content", None)
        return FakeWS(fn(*args, **kwargs))

    return wrapper


def real_exec_clients(monkeypatch, stdout: str) -> k8s_manager.Clients:
    class _Socket(FakeWS):
        def __init__(self, configuration, url, headers, capture_all, binary=False):
            super().__init__(stdout)

    monkeypatch.setattr(ws_client, "WSClient", _Socket)
    core = k8s_client.CoreV1Api(k8s_client.ApiClient(k8s_client.Configuration()))
    return k8s_manager.Clients(core=core, net=None)
