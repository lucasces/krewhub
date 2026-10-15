"""app/chp_client.py -- registro de rota no configurable-http-proxy via
`kubectl exec` + curl DENTRO do pod do CHP (a API admin do CHP só escuta
em 127.0.0.1 dentro do pod, ver docstring do módulo). Mockamos
`kubernetes.stream.stream` (import `from kubernetes.stream import
stream` em app/chp_client.py) pra capturar o comando shell exato que
seria executado -- sem nenhum kubectl/exec/HTTP real."""

from __future__ import annotations

from unittest import mock

import pytest

from app import chp_client, pod_exec
from app.config import Settings
from tests.ws_fake import wsfy


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
    """Achado documentado em AGENTS.md: a API do CHP usa
    `Authorization: token <valor>`, NÃO `Authorization: Bearer <valor>`
    -- um Bearer aqui dá 403 silencioso contra o CHP real."""
    captured = {}

    def _fake_stream(_exec_fn, pod_name, namespace, **kwargs):
        captured["pod_name"] = pod_name
        captured["namespace"] = namespace
        captured["command"] = kwargs["command"]
        return "\nHTTP_STATUS:201\n"

    monkeypatch.setattr(pod_exec, "stream", wsfy(_fake_stream))
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

    monkeypatch.setattr(pod_exec, "stream", wsfy(_fake_stream))
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

    monkeypatch.setattr(pod_exec, "stream", wsfy(_fake_stream))
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


def test_self_target_points_at_krewhub_central_in_chp_namespace():
    settings = _settings(chp_namespace="chp-ns", self_port=8080)
    assert (
        chp_client.self_target(settings)
        == "http://krewhub-central.chp-ns.svc.cluster.local:8080"
    )


def test_dev_target_points_at_kirocrew_pod_on_dashboard_port():
    assert (
        chp_client.dev_target("dev-a-test-local", "krewhub-devs")
        == f"http://kirocrew-dev-a-test-local.krewhub-devs.svc.cluster.local:{chp_client.DEV_POD_PORT}"
    )


def test_list_routes_parses_json_body_separate_from_http_status(monkeypatch, fake_clients):
    """A listagem separa o corpo JSON do `HTTP_STATUS` anexado via -w --
    o host-routing do CHP devolve as chaves com barra inicial."""
    captured = {}

    def _fake_stream(_exec_fn, pod_name, namespace, **kwargs):
        captured["command"] = kwargs["command"]
        body = '{"/krewhub.example.com": {"target": "http://krewhub-central:8080"}}'
        return f"{body}\nHTTP_STATUS:200\n"

    monkeypatch.setattr(pod_exec, "stream", wsfy(_fake_stream))
    routes = chp_client.list_routes(fake_clients, _settings())

    assert routes == {"/krewhub.example.com": {"target": "http://krewhub-central:8080"}}
    curl_cmd = captured["command"][-1]
    assert curl_cmd.rstrip().endswith("/api/routes")
    assert "-X POST" not in curl_cmd
    assert "Authorization: token $CONFIGPROXY_AUTH_TOKEN" in curl_cmd


def test_list_routes_returns_empty_dict_for_empty_table(monkeypatch, fake_clients):
    """Tabela vazia (o estado exato logo após um restart do pod do CHP)
    -- 200 com corpo `{}`, devolve dict vazio, NÃO levanta."""

    def _fake_stream(*_a, **_kw):
        return "{}\nHTTP_STATUS:200\n"

    monkeypatch.setattr(pod_exec, "stream", wsfy(_fake_stream))
    assert chp_client.list_routes(fake_clients, _settings()) == {}


def test_list_routes_raises_on_non_200(monkeypatch, fake_clients):
    def _fake_stream(*_a, **_kw):
        return "\nHTTP_STATUS:500\n"

    monkeypatch.setattr(pod_exec, "stream", wsfy(_fake_stream))
    with pytest.raises(chp_client.CHPError, match="500"):
        chp_client.list_routes(fake_clients, _settings())


def test_list_routes_raises_on_invalid_json(monkeypatch, fake_clients):
    def _fake_stream(*_a, **_kw):
        return "not json at all\nHTTP_STATUS:200\n"

    monkeypatch.setattr(pod_exec, "stream", wsfy(_fake_stream))
    with pytest.raises(chp_client.CHPError, match="JSON"):
        chp_client.list_routes(fake_clients, _settings())
