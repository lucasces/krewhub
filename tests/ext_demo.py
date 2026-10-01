"""Extensão de exemplo + fixtures compartilhadas pelos testes de runtime,
UI e endpoints das extensões (nenhum cluster real: k8s mockado)."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from app import extensions, k8s_manager
from app.extensions import base
from tests.test_k8s_manager import fake_clients  # noqa: F401


class DemoExtension(base.Extension):
    id = "demo"
    name = "Demo"
    description = "Extensão de teste."
    fields = (
        base.FieldSpec("url", "URL", required=True, pattern=r"https://\S+", default="https://demo.test"),
        base.FieldSpec("mode", "Modo", kind="select", options=("a", "b"), default="a"),
        base.FieldSpec("flag", "Flag", kind="bool"),
        base.FieldSpec("token", "Token", kind="secret", required=True),
        base.FieldSpec("auth", "Auth interna", kind="generated"),
    )
    actions = (
        base.ActionSpec("login", "Entrar", requires=("pod.ready",)),
        base.ActionSpec("sync", "Sincronizar", requires=("demo.authenticated",),
                        params=(base.FieldSpec("scope", "Escopo", kind="select", options=("x", "y"), default="x"),)),
    )

    def pod_contribution(self, ctx):
        return base.PodContribution(
            containers=[{"name": "demo", "image": "example/demo:1"}],
            main_env=[base.secret_env("DEMO_AUTH", ctx.slug, "demo", "auth")],
        )

    def status(self, ctx):
        authed = bool(ctx.state.get("authed"))
        return base.Status(
            conditions={"demo.authenticated": authed},
            card=base.Card(title="Demo", summary="logado" if authed else "sem login", code=ctx.state.get("code", "")),
        )

    def handle_action(self, ctx, action_id, params):
        if action_id == "login":
            return base.ActionResult(ok=True, message="logado", state_updates={"authed": True})
        if action_id == "sync":
            return base.ActionResult(ok=True, message=f"sync {params['scope']}")
        return super().handle_action(ctx, action_id, params)


class ExplodingExtension(DemoExtension):
    id = "boom"
    name = "Boom"

    def status(self, ctx):
        raise RuntimeError("segredo-interno")

    def handle_action(self, ctx, action_id, params):
        raise RuntimeError("segredo-interno")

    def on_pod_ready(self, ctx):
        raise RuntimeError("segredo-interno")


class _EP:
    def __init__(self, cls):
        self.name = cls.id
        self._cls = cls

    def load(self):
        return self._cls


@pytest.fixture(autouse=False)
def demo_installed():
    extensions.reset_for_tests(lambda: [_EP(DemoExtension), _EP(ExplodingExtension)])
    yield
    extensions.reset_for_tests()


@pytest.fixture
def ext_settings(settings, demo_installed):
    return dataclasses.replace(settings, extensions_enabled="demo")


def pod(*, ready=True, sidecars=("demo",)):
    def st(name, ok):
        return SimpleNamespace(
            name=name, ready=ok, state=SimpleNamespace(running=object() if ok else None)
        )

    return SimpleNamespace(
        status=SimpleNamespace(
            phase="Running" if ready else "Pending",
            container_statuses=[st("kirocrew", ready)] + [st(n, ready) for n in sidecars],
        )
    )


@pytest.fixture
def ready_pod(fake_clients):  # noqa: F811
    fake_clients.core.read_namespaced_pod.side_effect = None
    fake_clients.core.read_namespaced_pod.return_value = pod()
    return fake_clients


def enable_demo(settings, owner="dev-a@test.local", *, token_set=True, config=None):
    from app import store

    with store.connect(settings.db_path) as conn:
        store.upsert_extension(
            conn, owner, "demo", enabled=True, config=config or {"url": "https://demo.test"},
            secrets={"token": store.secret_marker()} if token_set else {},
        )
