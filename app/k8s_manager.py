"""Reconcile idempotente via API do k8s (client python `kubernetes`) --
substitui "editar YAML no repo Flux à mão + flux reconcile manual" pelas
fatias anteriores. Cada `ensure_*` faz create-se-não-existir /
patch-se-já-existir; chamar de novo com o mesmo owner_id não duplica nem
falha (idempotente).

Mudança de arquitetura desta fatia: todos os devs compartilham UM
namespace (`Settings.dev_namespace`) -- `reconcile_dev` não cria mais um
Namespace novo por owner_id, só garante (idempotente) que o namespace
compartilhado existe, e nomeia os demais recursos com o slug do dev pra
não colidir dentro dele. Ver k8s_templates.py pro detalhe de cada
manifest e app/main.py's docstring de `/lobby` pro fluxo completo."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

from app import k8s_templates as tpl
from app.config import Settings

logger = logging.getLogger("krewhub.k8s")

_config_loaded = False


@dataclass
class Clients:
    core: client.CoreV1Api
    net: client.NetworkingV1Api


def _load_config(settings: Settings) -> None:
    """In-cluster primeiro (ServiceAccount montado -- é como o serviço
    roda de verdade agora, dentro do namespace `kirohub`, sem depender do
    kubeconfig pessoal de ninguém), com fallback pro kubeconfig local (pra
    continuar dando pra rodar fora do cluster, como nas fatias
    anteriores). Cacheado num módulo-level flag -- carregar de novo em
    toda chamada é redundante (client python já mantém o Configuration
    global depois da primeira carga)."""
    global _config_loaded
    if _config_loaded:
        return
    try:
        config.load_incluster_config()
        logger.info("k8s config: in-cluster (ServiceAccount)")
    except ConfigException:
        config.load_kube_config(config_file=settings.k8s_kubeconfig, context=settings.k8s_context)
        logger.info("k8s config: kubeconfig local (%s, contexto %s)", settings.k8s_kubeconfig, settings.k8s_context)
    _config_loaded = True


def get_clients(settings: Settings) -> Clients:
    _load_config(settings)
    return Clients(
        core=client.CoreV1Api(),
        net=client.NetworkingV1Api(),
    )


def _ensure(*, read, create, patch, name, body, namespace=None) -> str:
    """create se 404, patch caso contrário. Mesmo padrão pros 3 tipos de
    recurso (cluster-scoped como Namespace, e namespaced como o resto) --
    só muda se `namespace` é passado ou não pro read/patch."""
    read_args = (name,) if namespace is None else (name, namespace)
    try:
        read(*read_args)
    except ApiException as exc:
        if exc.status == 404:
            create_args = (body,) if namespace is None else (namespace, body)
            create(*create_args)
            return "created"
        raise
    else:
        patch_args = (*read_args, body)
        patch(*patch_args)
        return "updated"


def ensure_dev_namespace(c: Clients, namespace: str) -> str:
    """Namespace COMPARTILHADO -- só CONFIRMA que existe (GET), nunca
    cria nem edita. Ele é criado declarativamente via GitOps (ver
    dev-namespace.yaml) -- diferente de todos os outros `ensure_*` (que
    seguem o padrão create-se-404/patch-se-existe via `_ensure()`), este
    é deliberadamente só-leitura: o RBAC do ServiceAccount não tem
    create/patch/update em `namespaces` (achado desta fatia: sobrava,
    reduzido). Se o namespace não existir, isso propaga a ApiException
    (404) do jeito que é -- erro explícito, não um fallback silencioso
    tentando criar."""
    c.core.read_namespace(namespace)
    return "exists"


def ensure_secret(c: Clients, namespace: str, slug: str, owner_id: str) -> str:
    body = tpl.build_secret(namespace, slug, owner_id)
    return _ensure(
        read=c.core.read_namespaced_secret,
        create=c.core.create_namespaced_secret,
        patch=c.core.patch_namespaced_secret,
        name=f"kiro-owner-id-{slug}",
        namespace=namespace,
        body=body,
    )


def ensure_configmap(c: Clients, namespace: str, slug: str, host: str, settings: Settings) -> str:
    body = tpl.build_configmap(namespace, slug, host, settings)
    return _ensure(
        read=c.core.read_namespaced_config_map,
        create=c.core.create_namespaced_config_map,
        patch=c.core.patch_namespaced_config_map,
        name=f"kiro-config-{slug}",
        namespace=namespace,
        body=body,
    )


def ensure_pvc(c: Clients, namespace: str, slug: str, settings: Settings) -> str:
    body = tpl.build_pvc(namespace, slug, settings)
    return _ensure(
        read=c.core.read_namespaced_persistent_volume_claim,
        create=c.core.create_namespaced_persistent_volume_claim,
        patch=c.core.patch_namespaced_persistent_volume_claim,
        name=f"kiro-workspace-{slug}",
        namespace=namespace,
        body=body,
    )


def ensure_service(c: Clients, namespace: str, slug: str) -> str:
    body = tpl.build_service(namespace, slug)
    return _ensure(
        read=c.core.read_namespaced_service,
        create=c.core.create_namespaced_service,
        patch=c.core.patch_namespaced_service,
        name=f"kirocrew-{slug}",
        namespace=namespace,
        body=body,
    )


def ensure_networkpolicy(c: Clients, namespace: str, slug: str, settings: Settings) -> str:
    body = tpl.build_networkpolicy(namespace, slug, settings)
    return _ensure(
        read=c.net.read_namespaced_network_policy,
        create=c.net.create_namespaced_network_policy,
        patch=c.net.patch_namespaced_network_policy,
        name=f"allow-chp-to-dashboard-only-{slug}",
        namespace=namespace,
        body=body,
    )


def ensure_pod(c: Clients, namespace: str, slug: str, settings: Settings) -> str:
    body = tpl.build_pod(namespace, slug, settings)
    return _ensure(
        read=c.core.read_namespaced_pod,
        create=c.core.create_namespaced_pod,
        patch=c.core.patch_namespaced_pod,
        name=f"kirocrew-{slug}",
        namespace=namespace,
        body=body,
    )


def wait_for_ready(c: Clients, namespace: str, slug: str, *, timeout_s: int = 240, poll_s: int = 5) -> bool:
    """Antes desta fatia isso lia `read_namespaced_deployment_status` e
    conferia `status.ready_replicas` (semântica do ReplicaSet). Migrado
    pra ler o `Pod` diretamente (`read_namespaced_pod` -- de propósito,
    não `read_namespaced_pod_status`: assim o RBAC continua precisando
    só de `get` em `pods`, sem precisar de uma regra nova pro
    subrecurso `pods/status`) e considera Ready quando `status.phase ==
    "Running"` E todo container reportado em `status.container_statuses`
    está com `ready == True` (equivalente, pra 1 pod sem réplica, ao que
    `ready_replicas >= 1` verificava antes)."""
    deadline = time.time() + timeout_s
    name = f"kirocrew-{slug}"
    while time.time() < deadline:
        pod = c.core.read_namespaced_pod(name, namespace)
        status = pod.status
        container_statuses = status.container_statuses or []
        ready = (
            status.phase == "Running"
            and bool(container_statuses)
            and all(cs.ready for cs in container_statuses)
        )
        if ready:
            return True
        time.sleep(poll_s)
    return False


class TeardownError(RuntimeError):
    """Erro real (não-404) ao deletar um recurso do workload de um dev --
    nunca inclui PVC/Secret, só os 4 recursos que `teardown_dev_workload`
    remove (ver docstring). Espelha `session_client.SessionError`: nunca
    deixa uma `ApiException` crua vazar pra quem chama, sempre um erro
    já traduzido com o nome do recurso que falhou."""


def _delete_ignore_not_found(delete, name: str, namespace: str, *, resource: str) -> str:
    """`ignore_not_found` na mão -- o cliente `kubernetes` não tem um
    parâmetro pronto pra isso nos métodos `delete_namespaced_*` (existe
    só pro CLI `kubectl delete --ignore-not-found`, não pra API client).
    404 -- já não existe, conta como sucesso (é exatamente o que faz
    `teardown_dev_workload` idempotente: chamar de novo após já ter
    deletado tudo, ou parcialmente, nunca falha). Qualquer outro erro
    (RBAC, timeout, etc.) é real e propaga como `TeardownError` --
    nunca finge sucesso silenciosamente."""
    try:
        delete(name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return "already_absent"
        raise TeardownError(f"falha ao deletar {resource} {name!r} em {namespace}: {exc}") from exc
    return "deleted"


def teardown_dev_workload(c: Clients, namespace: str, slug: str) -> dict:
    """Contraparte de `reconcile_dev` -- deleta SÓ Pod, Service,
    NetworkPolicy e ConfigMap do dev (mesmos nomes determinísticos que os
    `ensure_*` usam pra criar/patchar), preservando EXPLICITAMENTE o PVC
    (`kiro-workspace-{slug}`) e o Secret (`kiro-owner-id-{slug}`) -- os
    dois nunca aparecem aqui, de propósito. É isso que garante que um
    `reconcile_dev` seguinte (via `/provision`, `/open` ou `/lobby`)
    reconstrói o workload do zero de forma idempotente com o MESMO
    workspace/histórico/login do kiro-cli (que vivem no PVC) -- é
    essencialmente um culling manual, por-dev, sob demanda (o culling
    automático por inatividade continua pendente, ver README).

    Idempotente via `_delete_ignore_not_found` em cada recurso
    individualmente -- chamar de novo depois de já ter deletado tudo (ou
    só parte, se uma chamada anterior falhou no meio) nunca falha: cada
    recurso ausente conta como já removido, não como erro. Levanta
    `TeardownError` só se a delecão de algum recurso falhar por um
    motivo real (não-404) -- nesse caso os recursos já deletados ANTES do
    que falhou continuam deletados (sem rollback), e quem chama pode
    tentar de novo (idempotente)."""
    steps = {
        "pod": _delete_ignore_not_found(
            c.core.delete_namespaced_pod, f"kirocrew-{slug}", namespace, resource="Pod"
        ),
        "service": _delete_ignore_not_found(
            c.core.delete_namespaced_service, f"kirocrew-{slug}", namespace, resource="Service"
        ),
        "networkpolicy": _delete_ignore_not_found(
            c.net.delete_namespaced_network_policy,
            f"allow-chp-to-dashboard-only-{slug}",
            namespace,
            resource="NetworkPolicy",
        ),
        "configmap": _delete_ignore_not_found(
            c.core.delete_namespaced_config_map, f"kiro-config-{slug}", namespace, resource="ConfigMap"
        ),
    }
    logger.info(
        "teardown namespace=%s slug=%s steps=%s (PVC kiro-workspace-%s e Secret kiro-owner-id-%s preservados)",
        namespace, slug, steps, slug, slug,
    )
    return {"namespace": namespace, "slug": slug, "steps": steps}


def reconcile_dev(settings: Settings, owner_id: str) -> dict:
    """Idempotente: chamar de novo com o mesmo owner_id reaplica (patch) em
    vez de duplicar. Retorna o resultado ANTES de esperar o pod ficar
    Ready -- quem chama decide se quer aguardar (`wait_for_ready`).

    Namespace é sempre `settings.dev_namespace` (COMPARTILHADO entre
    todos os devs) -- não é mais derivado do slug do owner_id. Os demais
    recursos levam o slug no nome pra coexistir no mesmo namespace sem
    colidir."""
    slug = tpl.slugify(owner_id)
    namespace = settings.dev_namespace
    host = tpl.host_for(slug, settings)

    c = get_clients(settings)
    steps = {
        "namespace": ensure_dev_namespace(c, namespace),
        "secret": ensure_secret(c, namespace, slug, owner_id),
        "configmap": ensure_configmap(c, namespace, slug, host, settings),
        "pvc": ensure_pvc(c, namespace, slug, settings),
        "service": ensure_service(c, namespace, slug),
        "networkpolicy": ensure_networkpolicy(c, namespace, slug, settings),
        "pod": ensure_pod(c, namespace, slug, settings),
    }
    logger.info("reconcile owner_id=%s namespace=%s slug=%s steps=%s", owner_id, namespace, slug, steps)
    return {"owner_id": owner_id, "slug": slug, "namespace": namespace, "host": host, "steps": steps}
