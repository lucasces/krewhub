"""app/session_client.py -- `kirocrew token`/`kirocrew logout` via
`kubectl exec` (sem pty). `kubernetes.stream.stream` mockado -- nenhum
pod real."""

from __future__ import annotations

from unittest import mock

import pytest

from app import session_client
from app.k8s_templates import OWNER_LABEL_KEY


def _pod(name: str, *, phase: str = "Running") -> mock.Mock:
    pod = mock.Mock()
    pod.metadata.name = name
    pod.status.phase = phase
    return pod


@pytest.fixture
def fake_clients():
    return mock.Mock()


def test_find_kirocrew_pod_filters_by_slug_not_just_app_label(fake_clients):
    """Namespace compartilhado: o selector precisa incluir o slug do
    dev, senão "app=kirocrew" sozinho bateria com o pod de QUALQUER
    outro dev no mesmo namespace (achado documentado em AGENTS.md, seção
    "Architecture")."""
    fake_clients.core.list_namespaced_pod.return_value = mock.Mock(
        items=[_pod("kirocrew-dev-a-abc")]
    )
    pod_name = session_client._find_kirocrew_pod(fake_clients, "krewhub-devs", "dev-a-test-local")
    assert pod_name == "kirocrew-dev-a-abc"

    _, kwargs = fake_clients.core.list_namespaced_pod.call_args
    assert kwargs["label_selector"] == f"app=kirocrew,{OWNER_LABEL_KEY}=dev-a-test-local"


def test_find_kirocrew_pod_ignores_non_running_pods(fake_clients):
    fake_clients.core.list_namespaced_pod.return_value = mock.Mock(
        items=[_pod("kirocrew-dev-a-old", phase="Terminating"), _pod("kirocrew-dev-a-new")]
    )
    pod_name = session_client._find_kirocrew_pod(fake_clients, "krewhub-devs", "dev-a-test-local")
    assert pod_name == "kirocrew-dev-a-new"


def test_find_kirocrew_pod_raises_when_none_running(fake_clients):
    fake_clients.core.list_namespaced_pod.return_value = mock.Mock(items=[])
    with pytest.raises(session_client.SessionError, match="nenhum pod"):
        session_client._find_kirocrew_pod(fake_clients, "krewhub-devs", "dev-a-test-local")


def test_issue_token_url_rewrites_internal_url_to_public_host(monkeypatch, fake_clients):
    fake_clients.core.list_namespaced_pod.return_value = mock.Mock(
        items=[_pod("kirocrew-dev-a-abc")]
    )

    captured = {}

    def _fake_stream(_exec_fn, pod_name, namespace, **kwargs):
        captured["command"] = kwargs["command"]
        return "http://localhost:5476?token=THE-TOKEN-VALUE\n"

    monkeypatch.setattr(session_client, "stream", _fake_stream)

    url = session_client.issue_token_url(
        fake_clients,
        namespace="krewhub-devs",
        slug="dev-a-test-local",
        host="dev-a-test-local.kiro.internal",
        public_port="8080",
        ttl="30m",
    )
    assert url == "http://dev-a-test-local.kiro.internal:8080/?token=THE-TOKEN-VALUE"
    assert captured["command"] == ["kirocrew", "token", "--ttl", "30m"]


def test_issue_token_url_default_scheme_is_http_unchanged(monkeypatch, fake_clients):
    """Sem `scheme=` explicito (comportamento pre-existente, chamado sem
    esse kwarg), tem que continuar gerando http:// -- sem regressao pro
    homelab, que nunca passa scheme nenhum."""
    fake_clients.core.list_namespaced_pod.return_value = mock.Mock(
        items=[_pod("kirocrew-dev-a-abc")]
    )
    monkeypatch.setattr(
        session_client,
        "stream",
        lambda *a, **kw: "http://localhost:5476?token=THE-TOKEN-VALUE\n",
    )
    url = session_client.issue_token_url(
        fake_clients,
        namespace="krewhub-devs",
        slug="dev-a-test-local",
        host="dev-a-test-local.kiro.internal",
        public_port="8080",
    )
    assert url == "http://dev-a-test-local.kiro.internal:8080/?token=THE-TOKEN-VALUE"


def test_issue_token_url_https_scheme_when_tls_terminates_at_the_edge(monkeypatch, fake_clients):
    """TLS termina na borda (Ingress/ALB) -- dashboard_url_with_token
    precisa vir com https:// (mesmo achado do KIROCREW_CORS_ORIGINS:
    scheme errado quebra o link/CSRF mesmo com host/porta certos)."""
    fake_clients.core.list_namespaced_pod.return_value = mock.Mock(
        items=[_pod("kirocrew-dev-a-abc")]
    )
    monkeypatch.setattr(
        session_client,
        "stream",
        lambda *a, **kw: "http://localhost:5476?token=THE-TOKEN-VALUE\n",
    )
    url = session_client.issue_token_url(
        fake_clients,
        namespace="krewhub-devs",
        slug="dev-a-test-local",
        host="dev-a-test-local.kiro.example.internal",
        public_port="443",
        scheme="https",
    )
    assert url == "https://dev-a-test-local.kiro.example.internal:443/?token=THE-TOKEN-VALUE"


def test_issue_token_url_raises_when_no_token_in_output(monkeypatch, fake_clients):
    fake_clients.core.list_namespaced_pod.return_value = mock.Mock(
        items=[_pod("kirocrew-dev-a-abc")]
    )
    monkeypatch.setattr(session_client, "stream", lambda *a, **kw: "algo deu errado, sem URL nenhuma")
    with pytest.raises(session_client.SessionError):
        session_client.issue_token_url(
            fake_clients,
            namespace="krewhub-devs",
            slug="dev-a-test-local",
            host="x.kiro.internal",
            public_port="8080",
        )


def test_revoke_session_runs_kirocrew_logout_and_confirms_success(monkeypatch, fake_clients):
    fake_clients.core.list_namespaced_pod.return_value = mock.Mock(
        items=[_pod("kirocrew-dev-a-abc")]
    )
    captured = {}

    def _fake_stream(_exec_fn, pod_name, namespace, **kwargs):
        captured["command"] = kwargs["command"]
        captured["pod_name"] = pod_name
        captured["namespace"] = namespace
        return "✅ All dashboard sessions revoked.\n"

    monkeypatch.setattr(session_client, "stream", _fake_stream)
    result = session_client.revoke_session(fake_clients, namespace="krewhub-devs", slug="dev-a-test-local")

    assert "revoked" in result
    assert captured["command"] == ["kirocrew", "logout"]
    assert captured["pod_name"] == "kirocrew-dev-a-abc"
    assert captured["namespace"] == "krewhub-devs"


def test_revoke_session_raises_when_no_success_marker(monkeypatch, fake_clients):
    """Nunca finge sucesso -- se `kirocrew logout` não imprimir o
    marcador de sucesso (ex.: "Gateway not running"), levanta
    SessionError."""
    fake_clients.core.list_namespaced_pod.return_value = mock.Mock(
        items=[_pod("kirocrew-dev-a-abc")]
    )
    monkeypatch.setattr(
        session_client, "stream", lambda *a, **kw: "❌ Gateway not running — start it with: kirocrew gateway"
    )
    with pytest.raises(session_client.SessionError, match="n.o confirmou sucesso"):
        session_client.revoke_session(fake_clients, namespace="krewhub-devs", slug="dev-a-test-local")


def test_revoke_session_uses_slug_scoped_pod_not_other_dev(monkeypatch, fake_clients):
    """Revogar a sessão do dev A nunca pode acabar executando `kirocrew
    logout` no pod do dev B -- mesmo filtro por slug do
    `_find_kirocrew_pod`."""
    fake_clients.core.list_namespaced_pod.return_value = mock.Mock(
        items=[_pod("kirocrew-dev-a-abc")]
    )
    monkeypatch.setattr(session_client, "stream", lambda *a, **kw: "✅ All dashboard sessions revoked.")
    session_client.revoke_session(fake_clients, namespace="krewhub-devs", slug="dev-a-test-local")

    _, kwargs = fake_clients.core.list_namespaced_pod.call_args
    assert kwargs["label_selector"] == f"app=kirocrew,{OWNER_LABEL_KEY}=dev-a-test-local"
