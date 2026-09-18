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
    apps: client.AppsV1Api
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
        apps=client.AppsV1Api(),
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


def ensure_deployment(c: Clients, namespace: str, slug: str, settings: Settings) -> str:
    body = tpl.build_deployment(namespace, slug, settings)
    return _ensure(
        read=c.apps.read_namespaced_deployment,
        create=c.apps.create_namespaced_deployment,
        patch=c.apps.patch_namespaced_deployment,
        name=f"kirocrew-{slug}",
        namespace=namespace,
        body=body,
    )


def wait_for_ready(c: Clients, namespace: str, slug: str, *, timeout_s: int = 240, poll_s: int = 5) -> bool:
    deadline = time.time() + timeout_s
    name = f"kirocrew-{slug}"
    while time.time() < deadline:
        dep = c.apps.read_namespaced_deployment_status(name, namespace)
        ready = dep.status.ready_replicas or 0
        if ready >= 1:
            return True
        time.sleep(poll_s)
    return False


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
        "deployment": ensure_deployment(c, namespace, slug, settings),
    }
    logger.info("reconcile owner_id=%s namespace=%s slug=%s steps=%s", owner_id, namespace, slug, steps)
    return {"owner_id": owner_id, "slug": slug, "namespace": namespace, "host": host, "steps": steps}
