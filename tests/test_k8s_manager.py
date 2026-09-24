"""app/k8s_manager.py -- reconcile idempotente (create-se-404/patch-se-
existe) via API do k8s. `Clients` (core/net) e' 100% mockado
(unittest.mock.MagicMock) -- nenhuma chamada de rede/cluster real.
`k8s_manager.get_clients` tambem e' monkeypatchado pra nao tentar
carregar kubeconfig/in-cluster config nenhum.

Migracao desta fatia (ver README, secao "Deployment vs Pod puro pro
workload por-dev"): o workload por-dev deixou de ser um `Deployment`
(client `apps.AppsV1Api`) e virou um `Pod` puro (client `core.CoreV1Api`,
o mesmo ja usado pra Secret/ConfigMap/Service/PVC) -- `Clients` perdeu o
campo `apps` (nada mais no codigo usa `AppsV1Api`)."""

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
        dev_pod_scheme="http",
        kirocrew_image="ghcr.io/kirodotdev/kirocrew:0.6.0",
        storage_class="rook-cephfs",
        storage_size="10Gi",
        chp_namespace="kirohub",
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


def _not_found() -> ApiException:
    return ApiException(status=404, reason="Not Found")


def _running_pod_with_ready_containers() -> mock.Mock:
    pod = mock.Mock()
    pod.status.phase = "Running"
    container = mock.Mock()
    container.ready = True
    pod.status.container_statuses = [container]
    return pod


@pytest.fixture
def fake_clients(monkeypatch):
    """`Clients` com core/net mockados -- `read_*` levanta 404 por
    padrão (força o caminho `create`); testes que querem simular
    "já existe" reconfiguram o mock pra retornar em vez de levantar."""
    clients = k8s_manager.Clients(core=mock.MagicMock(), net=mock.MagicMock())
    for read_method in (
        clients.core.read_namespaced_secret,
        clients.core.read_namespaced_config_map,
        clients.core.read_namespaced_persistent_volume_claim,
        clients.core.read_namespaced_service,
        clients.net.read_namespaced_network_policy,
        clients.core.read_namespaced_pod,
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
        "pod": "created",
    }
    slug = result["slug"]
    fake_clients.core.create_namespaced_secret.assert_called_once()
    assert fake_clients.core.create_namespaced_secret.call_args[0][0] == "krewhub-devs"
    created_secret_body = fake_clients.core.create_namespaced_secret.call_args[0][1]
    assert created_secret_body["metadata"]["name"] == f"kiro-owner-id-{slug}"

    fake_clients.core.create_namespaced_pod.assert_called_once()
    created_pod_body = fake_clients.core.create_namespaced_pod.call_args[0][1]
    assert created_pod_body["metadata"]["name"] == f"kirocrew-{slug}"
    assert created_pod_body["kind"] == "Pod"

    # Nada foi "patched" na primeira vez -- tudo criado do zero.
    fake_clients.core.patch_namespaced_secret.assert_not_called()
    fake_clients.core.patch_namespaced_pod.assert_not_called()


def test_reconcile_dev_second_call_patches_instead_of_duplicating(fake_clients):
    """Idempotência ponta a ponta: reconciliar duas vezes com o MESMO
    owner_id não recria nada -- create_* é chamado só na 1a vez,
    patch_* na 2a, sempre com o MESMO nome determinístico (achado
    validado ao vivo, replicado aqui como regressão rápida)."""
    settings = _settings()

    first = k8s_manager.reconcile_dev(settings, "dev-a@test.local")
    assert first["steps"]["secret"] == "created"
    assert first["steps"]["pod"] == "created"

    # Segunda chamada: "já existe" -- read_* passam a retornar em vez de
    # levantar 404.
    for read_method in (
        fake_clients.core.read_namespaced_secret,
        fake_clients.core.read_namespaced_config_map,
        fake_clients.core.read_namespaced_persistent_volume_claim,
        fake_clients.core.read_namespaced_service,
        fake_clients.net.read_namespaced_network_policy,
        fake_clients.core.read_namespaced_pod,
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
        "pod": "updated",
    }

    # create_* continua tendo sido chamado só 1 vez no total (não
    # duplicou na 2a chamada).
    assert fake_clients.core.create_namespaced_secret.call_count == 1
    assert fake_clients.core.create_namespaced_pod.call_count == 1

    # patch_* foi chamado com o MESMO nome que o create original usou.
    fake_clients.core.patch_namespaced_pod.assert_called_once()
    patch_name = fake_clients.core.patch_namespaced_pod.call_args[0][0]
    create_body = fake_clients.core.create_namespaced_pod.call_args[0][1]
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


# ---------------------------------------------------------------------------
# wait_for_ready -- migrado nesta fatia de "ler status.ready_replicas do
# Deployment" (read_namespaced_deployment_status) pra "ler o Pod direto e
# conferir phase=Running + todo container_status.ready" (read_namespaced_pod
# -- de propósito, não read_namespaced_pod_status, pra não exigir uma regra
# de RBAC nova pro subrecurso pods/status).
# ---------------------------------------------------------------------------


def test_wait_for_ready_returns_true_once_pod_running_with_all_containers_ready(fake_clients):
    pod_not_ready = mock.Mock()
    pod_not_ready.status.phase = "Pending"
    pod_not_ready.status.container_statuses = None

    pod_ready = _running_pod_with_ready_containers()

    fake_clients.core.read_namespaced_pod.side_effect = [pod_not_ready, pod_ready]

    ok = k8s_manager.wait_for_ready(fake_clients, "krewhub-devs", "dev-a-test-local", timeout_s=5, poll_s=0)
    assert ok is True


def test_wait_for_ready_running_phase_with_a_not_ready_container_is_not_ready(fake_clients):
    """phase=Running sozinho não basta -- todo container reportado
    precisa estar `ready=True` (equivalente, pra 1 pod sem réplica, ao
    que `ready_replicas >= 1` verificava antes pro Deployment)."""
    pod = mock.Mock()
    pod.status.phase = "Running"
    not_ready_container = mock.Mock()
    not_ready_container.ready = False
    pod.status.container_statuses = [not_ready_container]
    fake_clients.core.read_namespaced_pod.return_value = pod

    ok = k8s_manager.wait_for_ready(fake_clients, "krewhub-devs", "dev-a-test-local", timeout_s=0, poll_s=0)
    assert ok is False


def test_wait_for_ready_times_out_returns_false(fake_clients):
    pod_not_ready = mock.Mock()
    pod_not_ready.status.phase = "Pending"
    pod_not_ready.status.container_statuses = None
    fake_clients.core.read_namespaced_pod.return_value = pod_not_ready

    ok = k8s_manager.wait_for_ready(fake_clients, "krewhub-devs", "dev-a-test-local", timeout_s=0, poll_s=0)
    assert ok is False


# ---------------------------------------------------------------------------
# teardown_dev_workload -- contraparte de reconcile_dev, deleta SÓ
# Pod/Service/NetworkPolicy/ConfigMap, preserva PVC/Secret.
# ---------------------------------------------------------------------------


def test_teardown_dev_workload_deletes_only_the_four_resources_never_pvc_or_secret(fake_clients):
    result = k8s_manager.teardown_dev_workload(fake_clients, "krewhub-devs", "dev-a-test-local")

    assert result == {
        "namespace": "krewhub-devs",
        "slug": "dev-a-test-local",
        "steps": {
            "pod": "deleted",
            "service": "deleted",
            "networkpolicy": "deleted",
            "configmap": "deleted",
        },
    }
    fake_clients.core.delete_namespaced_pod.assert_called_once_with(
        "kirocrew-dev-a-test-local", "krewhub-devs"
    )
    fake_clients.core.delete_namespaced_service.assert_called_once_with(
        "kirocrew-dev-a-test-local", "krewhub-devs"
    )
    fake_clients.net.delete_namespaced_network_policy.assert_called_once_with(
        "allow-chp-to-dashboard-only-dev-a-test-local", "krewhub-devs"
    )
    fake_clients.core.delete_namespaced_config_map.assert_called_once_with(
        "kiro-config-dev-a-test-local", "krewhub-devs"
    )
    # Ponto crítico do requisito: PVC e Secret NUNCA são deletados aqui.
    fake_clients.core.delete_namespaced_persistent_volume_claim.assert_not_called()
    fake_clients.core.delete_namespaced_secret.assert_not_called()


def test_teardown_dev_workload_is_idempotent_when_resources_already_absent(fake_clients):
    """Chamar de novo depois de já ter deletado tudo (404 em todo mundo)
    não falha -- cada recurso ausente conta como já removido."""
    fake_clients.core.delete_namespaced_pod.side_effect = _not_found()
    fake_clients.core.delete_namespaced_service.side_effect = _not_found()
    fake_clients.net.delete_namespaced_network_policy.side_effect = _not_found()
    fake_clients.core.delete_namespaced_config_map.side_effect = _not_found()

    result = k8s_manager.teardown_dev_workload(fake_clients, "krewhub-devs", "dev-a-test-local")

    assert result["steps"] == {
        "pod": "already_absent",
        "service": "already_absent",
        "networkpolicy": "already_absent",
        "configmap": "already_absent",
    }


def test_teardown_dev_workload_raises_teardown_error_on_real_api_failure(fake_clients):
    """Erro não-404 (ex.: RBAC) é real -- propaga como TeardownError,
    nunca finge sucesso silenciosamente."""
    fake_clients.core.delete_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")

    with pytest.raises(k8s_manager.TeardownError):
        k8s_manager.teardown_dev_workload(fake_clients, "krewhub-devs", "dev-a-test-local")
