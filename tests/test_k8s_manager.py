"""app/k8s_manager.py -- reconcile idempotente (create-se-404/patch-se-
existe) via API do k8s. `Clients` (core/apps/net) e' 100% mockado
(unittest.mock.MagicMock) -- nenhuma chamada de rede/cluster real.
`k8s_manager.get_clients` tambem e' monkeypatchado pra nao tentar
carregar kubeconfig/in-cluster config nenhum."""

from __future__ import annotations

from unittest import mock

import pytest
from kubernetes.client.rest import ApiException

from app import k8s_manager
from app.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        k8s_kubeconfig="",
        k8s_context="test",
        dev_namespace="krewhub-devs",
        base_domain="kiro.internal",
        public_port="8080",
        kirocrew_image="ghcr.io/kirodotdev/kirocrew:0.6.0",
        storage_class="rook-cephfs",
        storage_size="10Gi",
        chp_namespace="kirohub",
        chp_pod_label="app=configurable-http-proxy",
        chp_admin_port=8001,
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


def _not_found() -> ApiException:
    return ApiException(status=404, reason="Not Found")


@pytest.fixture
def fake_clients(monkeypatch):
    """`Clients` com core/apps/net mockados -- `read_*` levanta 404 por
    padrão (força o caminho `create`); testes que querem simular
    "já existe" reconfiguram o mock pra retornar em vez de levantar."""
    clients = k8s_manager.Clients(core=mock.MagicMock(), apps=mock.MagicMock(), net=mock.MagicMock())
    for read_method in (
        clients.core.read_namespaced_secret,
        clients.core.read_namespaced_config_map,
        clients.core.read_namespaced_persistent_volume_claim,
        clients.core.read_namespaced_service,
        clients.net.read_namespaced_network_policy,
        clients.apps.read_namespaced_deployment,
    ):
        read_method.side_effect = _not_found()
    clients.core.read_namespace.return_value = mock.Mock()  # namespace compartilhado "existe"
    monkeypatch.setattr(k8s_manager, "get_clients", lambda settings: clients)
    return clients


def test_ensure_dev_namespace_is_read_only(fake_clients):
    """Achado desta sessão de trabalho (README, "Namespace único
    compartilhado"): o RBAC não tem create/patch em `namespaces` -- essa
    função só CONFIRMA que existe, nunca cria/edita."""
    status = k8s_manager.ensure_dev_namespace(fake_clients, "krewhub-devs")
    assert status == "exists"
    fake_clients.core.read_namespace.assert_called_once_with("krewhub-devs")
    fake_clients.core.create_namespace.assert_not_called()
    fake_clients.core.patch_namespace.assert_not_called()


def test_ensure_dev_namespace_propagates_404_when_missing(fake_clients):
    """Se o namespace compartilhado não existe (deveria ter sido criado
    via GitOps), o erro precisa vazar explícito -- não um fallback
    silencioso tentando criar."""
    fake_clients.core.read_namespace.side_effect = _not_found()
    with pytest.raises(ApiException):
        k8s_manager.ensure_dev_namespace(fake_clients, "krewhub-devs")


def test_reconcile_dev_first_call_creates_all_seven_resources(fake_clients):
    settings = _settings()
    result = k8s_manager.reconcile_dev(settings, "dev-a@test.local")

    assert result["steps"] == {
        "namespace": "exists",
        "secret": "created",
        "configmap": "created",
        "pvc": "created",
        "service": "created",
        "networkpolicy": "created",
        "deployment": "created",
    }
    slug = result["slug"]
    fake_clients.core.create_namespaced_secret.assert_called_once()
    assert fake_clients.core.create_namespaced_secret.call_args[0][0] == "krewhub-devs"
    created_secret_body = fake_clients.core.create_namespaced_secret.call_args[0][1]
    assert created_secret_body["metadata"]["name"] == f"kiro-owner-id-{slug}"

    fake_clients.apps.create_namespaced_deployment.assert_called_once()
    created_deploy_body = fake_clients.apps.create_namespaced_deployment.call_args[0][1]
    assert created_deploy_body["metadata"]["name"] == f"kirocrew-{slug}"

    # Nada foi "patched" na primeira vez -- tudo criado do zero.
    fake_clients.core.patch_namespaced_secret.assert_not_called()
    fake_clients.apps.patch_namespaced_deployment.assert_not_called()


def test_reconcile_dev_second_call_patches_instead_of_duplicating(fake_clients):
    """Idempotência ponta a ponta: reconciliar duas vezes com o MESMO
    owner_id não recria nada -- create_* é chamado só na 1a vez,
    patch_* na 2a, sempre com o MESMO nome determinístico (achado
    validado ao vivo, replicado aqui como regressão rápida)."""
    settings = _settings()

    first = k8s_manager.reconcile_dev(settings, "dev-a@test.local")
    assert first["steps"]["secret"] == "created"
    assert first["steps"]["deployment"] == "created"

    # Segunda chamada: "já existe" -- read_* passam a retornar em vez de
    # levantar 404.
    for read_method in (
        fake_clients.core.read_namespaced_secret,
        fake_clients.core.read_namespaced_config_map,
        fake_clients.core.read_namespaced_persistent_volume_claim,
        fake_clients.core.read_namespaced_service,
        fake_clients.net.read_namespaced_network_policy,
        fake_clients.apps.read_namespaced_deployment,
    ):
        read_method.side_effect = None
        read_method.return_value = mock.Mock()

    second = k8s_manager.reconcile_dev(settings, "dev-a@test.local")
    assert second["steps"] == {
        "namespace": "exists",
        "secret": "updated",
        "configmap": "updated",
        "pvc": "updated",
        "service": "updated",
        "networkpolicy": "updated",
        "deployment": "updated",
    }

    # create_* continua tendo sido chamado só 1 vez no total (não
    # duplicou na 2a chamada).
    assert fake_clients.core.create_namespaced_secret.call_count == 1
    assert fake_clients.apps.create_namespaced_deployment.call_count == 1

    # patch_* foi chamado com o MESMO nome que o create original usou.
    fake_clients.apps.patch_namespaced_deployment.assert_called_once()
    patch_name = fake_clients.apps.patch_namespaced_deployment.call_args[0][0]
    create_body = fake_clients.apps.create_namespaced_deployment.call_args[0][1]
    assert patch_name == create_body["metadata"]["name"]
    assert first["slug"] == second["slug"]


def test_reconcile_dev_uses_shared_namespace_for_every_owner(fake_clients):
    """Todos os devs vão pro MESMO namespace (`Settings.dev_namespace`)
    -- não um namespace novo por owner_id (arquitetura desta sessão de
    trabalho)."""
    settings = _settings(dev_namespace="krewhub-devs")
    result_a = k8s_manager.reconcile_dev(settings, "dev-a@test.local")
    result_b = k8s_manager.reconcile_dev(settings, "dev-b@test.local")
    assert result_a["namespace"] == result_b["namespace"] == "krewhub-devs"
    assert result_a["slug"] != result_b["slug"]


def test_wait_for_ready_returns_true_once_ready_replicas_positive(fake_clients):
    status_not_ready = mock.Mock()
    status_not_ready.status.ready_replicas = 0
    status_ready = mock.Mock()
    status_ready.status.ready_replicas = 1
    fake_clients.apps.read_namespaced_deployment_status.side_effect = [status_not_ready, status_ready]

    ok = k8s_manager.wait_for_ready(fake_clients, "krewhub-devs", "dev-a-test-local", timeout_s=5, poll_s=0)
    assert ok is True


def test_wait_for_ready_times_out_returns_false(fake_clients):
    status_not_ready = mock.Mock()
    status_not_ready.status.ready_replicas = 0
    fake_clients.apps.read_namespaced_deployment_status.return_value = status_not_ready

    ok = k8s_manager.wait_for_ready(fake_clients, "krewhub-devs", "dev-a-test-local", timeout_s=0, poll_s=0)
    assert ok is False
