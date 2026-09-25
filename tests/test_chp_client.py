"""app/chp_client.py -- registro de rota no configurable-http-proxy via
`kubectl exec` + curl DENTRO do pod do CHP (a API admin do CHP só escuta
em 127.0.0.1 dentro do pod, ver docstring do módulo). Mockamos
`kubernetes.stream.stream` (import `from kubernetes.stream import
stream` em app/chp_client.py) pra capturar o comando shell exato que
seria executado -- sem nenhum kubectl/exec/HTTP real."""

from __future__ import annotations

from unittest import mock

import pytest

from app import chp_client
from app.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        k8s_kubeconfig="",
        k8s_context="test",
        dev_namespace="krewhub-devs",
        base_domain="kiro.internal",
        public_port="8080",
        dev_pod_scheme="http",
        kirocrew_image="img",
        storage_class="sc",
        storage_size="1Gi",
        chp_namespace="chp-ns",
        chp_pod_label="app=configurable-http-proxy",
        chp_admin_port=8001,
        dev_pod_overlay_path="",
        dev_pod_overlay_json="",
        db_path=":memory:",
        session_ttl="24h",
        oidc_issuer="",
        oidc_client_id="",
        oidc_client_secret="",
        oidc_redirect_uri="",
        oidc_scopes="openid email profile",
        session_secret="s",
        auth_token_ttl_seconds=3600,
        kiro_identity_provider="",
        kiro_region="",
        self_host="",
        self_port=8080,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def fake_clients():
    clients = mock.Mock()
    pod = mock.Mock()
    pod.metadata.name = "configurable-http-proxy-abc123"
    clients.core.list_namespaced_pod.return_value = mock.Mock(items=[pod])
    return clients


def test_register_route_uses_token_auth_header_not_bearer(monkeypatch, fake_clients):
    """Achado documentado no README: a API do CHP usa
    `Authorization: token <valor>`, NÃO `Authorization: Bearer <valor>`
    -- um Bearer aqui dá 403 silencioso contra o CHP real."""
    captured = {}

    def _fake_stream(_exec_fn, pod_name, namespace, **kwargs):
        captured["pod_name"] = pod_name
        captured["namespace"] = namespace
        captured["command"] = kwargs["command"]
        return "\nHTTP_STATUS:201\n"

    monkeypatch.setattr(chp_client, "stream", _fake_stream)
    settings = _settings()
    result = chp_client.register_route(
        fake_clients, settings, host="dev-a-test-local.kiro.internal", target="http://svc:5476"
    )

    assert result["status"] == 201
    curl_cmd = captured["command"][-1]  # ["sh", "-c", curl_cmd]
    assert 'Authorization: token $CONFIGPROXY_AUTH_TOKEN' in curl_cmd
    assert "Bearer" not in curl_cmd
    assert captured["namespace"] == settings.chp_namespace
    assert captured["pod_name"] == "configurable-http-proxy-abc123"


def test_register_route_payload_targets_expected_host_from_slug(monkeypatch, fake_clients):
    from app import k8s_templates as tpl

    captured = {}

    def _fake_stream(_exec_fn, pod_name, namespace, **kwargs):
        captured["command"] = kwargs["command"]
        return "\nHTTP_STATUS:201\n"

    monkeypatch.setattr(chp_client, "stream", _fake_stream)
    settings = _settings(base_domain="kiro.internal")
    owner_id = "dev-a@test.local"
    slug = tpl.slugify(owner_id)
    host = tpl.host_for(slug, settings)
    target = f"http://kirocrew-{slug}.krewhub-devs.svc.cluster.local:5476"

    chp_client.register_route(fake_clients, settings, host=host, target=target)

    curl_cmd = captured["command"][-1]
    assert f"http://localhost:{settings.chp_admin_port}/api/routes/{host}/" in curl_cmd
    assert target in curl_cmd
    assert '"target"' in curl_cmd


def test_register_route_raises_on_non_2xx_status(monkeypatch, fake_clients):
    def _fake_stream(*_a, **_kw):
        return "\nHTTP_STATUS:500\n"

    monkeypatch.setattr(chp_client, "stream", _fake_stream)
    settings = _settings()
    with pytest.raises(chp_client.CHPError, match="500"):
        chp_client.register_route(fake_clients, settings, host="x.kiro.internal", target="http://y:5476")


def test_register_route_raises_when_no_chp_pod_found():
    settings = _settings()
    clients = mock.Mock()
    clients.core.list_namespaced_pod.return_value = mock.Mock(items=[])
    with pytest.raises(chp_client.CHPError, match="nenhum pod"):
        chp_client.register_route(clients, settings, host="x.kiro.internal", target="http://y:5476")


def test_parse_status_raises_when_marker_missing():
    with pytest.raises(chp_client.CHPError):
        chp_client._parse_status("resposta sem o marcador esperado")
