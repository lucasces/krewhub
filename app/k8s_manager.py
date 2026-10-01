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
from datetime import datetime

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

from app import k8s_templates as tpl
from app.config import Settings
from app.extensions.base import secret_name
from app.extensions.contributions import ExtPlan, collect_files, files_configmap_name

logger = logging.getLogger("krewhub.k8s")

_config_loaded = False


@dataclass
class Clients:
    core: client.CoreV1Api
    net: client.NetworkingV1Api


def _load_config(settings: Settings) -> None:
    """In-cluster primeiro (ServiceAccount montado -- é como o serviço
    roda de verdade hoje, dentro do namespace configurado via
    `KREWHUB_CHP_NAMESPACE`, sem depender do kubeconfig pessoal de
    ninguém), com fallback pro kubeconfig local (pra continuar dando pra
    rodar fora do cluster, como nas fatias anteriores). `k8s_context`
    vazio (default) vira `None` pra lib `kubernetes` -- usa o
    `current-context` já ativo no kubeconfig, sem exigir um nome de
    cluster específico hardcoded; só importa nesse fallback local, o
    path in-cluster nunca lê essa env var. Cacheado num módulo-level
    flag -- carregar de novo em toda chamada é redundante (client python
    já mantém o Configuration global depois da primeira carga)."""
    global _config_loaded
    if _config_loaded:
        return
    try:
        config.load_incluster_config()
        logger.info("k8s config: in-cluster (ServiceAccount)")
    except ConfigException:
        config.load_kube_config(config_file=settings.k8s_kubeconfig, context=settings.k8s_context or None)
        logger.info("k8s config: kubeconfig local (%s, contexto %s)", settings.k8s_kubeconfig, settings.k8s_context or "<current-context>")
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


def _ext_labels(slug: str) -> dict:
    return {tpl.OWNER_LABEL_KEY: slug, "app.kubernetes.io/managed-by": "krewhub"}


def _read_or_none(read, name: str, namespace: str):
    try:
        return read(name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def ensure_ext_secret(
    c: Clients,
    namespace: str,
    slug: str,
    *,
    set_values: dict[str, str] | None = None,
    generated: tuple[str, ...] = (),
    generate=None,
) -> str:
    """Secret `krewhub-ext-<slug>` com as chaves das extensões
    (`<ext_id>.<campo>`). `set_values` sobrescreve (valor informado pelo
    dev); chaves de `generated` só são geradas se ainda não existem --
    reprovisionar nunca troca um token que o Pod/sidecar já usa. Cria o
    Secret (mesmo vazio) se não existir. Devolve "created", "updated"
    ou "unchanged"."""
    from app.extensions.base import generate_secret

    gen = generate or generate_secret
    name = secret_name(slug)
    existing = _read_or_none(c.core.read_namespaced_secret, name, namespace)
    present = set((getattr(existing, "data", None) or {}).keys()) if existing is not None else set()
    string_data = dict(set_values or {})
    for key in generated:
        if key not in present and key not in string_data:
            string_data[key] = gen()

    if existing is None:
        c.core.create_namespaced_secret(
            namespace,
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": namespace, "labels": _ext_labels(slug)},
                "type": "Opaque",
                "stringData": string_data,
            },
        )
        return "created"
    if not string_data:
        return "unchanged"
    c.core.patch_namespaced_secret(name, namespace, {"stringData": string_data})
    return "updated"


def wipe_ext_secret_keys(
    c: Clients,
    namespace: str,
    slug: str,
    keys: tuple[str, ...] | list[str] | None = None,
    *,
    prefix: str | None = None,
) -> list[str]:
    """Remove chaves do Secret `krewhub-ext-<slug>` SEM `delete` no RBAC:
    JSON merge patch com `null` (RFC 7386) apaga a chave. `keys=None`
    apaga todas; `prefix` restringe às chaves que começam com ele
    (ex.: `"demo."` = tudo de uma extensão). Content-Type forçado -- o cliente python escolheria
    strategic-merge pra um body dict. Secret ausente = nada a fazer.
    Devolve as chaves realmente apagadas."""
    name = secret_name(slug)
    existing = _read_or_none(c.core.read_namespaced_secret, name, namespace)
    if existing is None:
        return []
    present = set((getattr(existing, "data", None) or {}).keys())
    targets = present if keys is None else present & set(keys)
    if prefix is not None:
        targets = {k for k in targets if k.startswith(prefix)}
    targets = sorted(targets)
    if not targets:
        return []
    c.core.patch_namespaced_secret(
        name,
        namespace,
        {"data": {k: None for k in targets}},
        _content_type="application/merge-patch+json",
    )
    return targets


def ensure_ext_files_configmap(c: Clients, namespace: str, slug: str, data: dict[str, str]) -> str:
    """ConfigMap `krewhub-ext-files-<slug>` com os arquivos das extensões
    (`<ext_id>.<arquivo>`). Sem arquivos nenhum, o ConfigMap é removido
    (nada monta). Chaves que sumiram são removidas do objeto existente."""
    name = files_configmap_name(slug)
    existing = _read_or_none(c.core.read_namespaced_config_map, name, namespace)
    if not data:
        if existing is None:
            return "absent"
        _delete_ignore_not_found(
            c.core.delete_namespaced_config_map, name, namespace, resource="ConfigMap"
        )
        return "deleted"
    if existing is None:
        c.core.create_namespaced_config_map(
            namespace,
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": namespace, "labels": _ext_labels(slug)},
                "data": data,
            },
        )
        return "created"
    stale = set((getattr(existing, "data", None) or {}).keys()) - set(data)
    body = {"data": {**data, **{k: None for k in stale}}}
    c.core.patch_namespaced_config_map(
        name, namespace, body, _content_type="application/merge-patch+json"
    )
    return "updated"


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


_POD_GONE_TIMEOUT_S = 120


def _wait_pod_gone(c: Clients, namespace: str, name: str, *, timeout_s: float, poll_s: float) -> None:
    deadline = time.time() + timeout_s
    while True:
        try:
            c.core.read_namespaced_pod(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise
        if time.time() >= deadline:
            raise TimeoutError(f"pod {name} ainda existe após {timeout_s}s do delete")
        time.sleep(poll_s)


def _recreate_pod(c: Clients, namespace: str, name: str, body: dict, *, poll_s: float) -> str:
    try:
        c.core.delete_namespaced_pod(name, namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
    _wait_pod_gone(c, namespace, name, timeout_s=_POD_GONE_TIMEOUT_S, poll_s=poll_s)
    c.core.create_namespaced_pod(namespace, body)
    return "recreated"


def ensure_pod(
    c: Clients, namespace: str, slug: str, settings: Settings, *, body: dict | None = None, poll_s: float = 2
) -> str:
    """create se não existe; patch se o spec não mudou; delete + espera
    sumir + create se mudou ("created"/"updated"/"recreated").

    O spec de um Pod é imutável -- patch com spec diferente dá 422. A
    anotação `krewhub.pespa.net/spec-hash` guarda o hash do spec com que
    o Pod foi criado. Pod sem a anotação (criado por versão anterior) tenta
    o patch e só recria se o apiserver recusar com 422. A recriação
    interrompe o dev (workspace fica no PVC); ver `docs/ARCHITECTURE.md`."""
    if body is None:
        body = tpl.build_pod(namespace, slug, settings)
    name = f"kirocrew-{slug}"
    try:
        existing = c.core.read_namespaced_pod(name, namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        c.core.create_namespaced_pod(namespace, body)
        return "created"

    if isinstance(existing.metadata.deletion_timestamp, datetime):
        return _recreate_pod(c, namespace, name, body, poll_s=poll_s)

    annotations = existing.metadata.annotations
    current = annotations.get(tpl.SPEC_HASH_ANNOTATION) if isinstance(annotations, dict) else None
    wanted = body["metadata"]["annotations"][tpl.SPEC_HASH_ANNOTATION]
    if current is not None and current != wanted:
        return _recreate_pod(c, namespace, name, body, poll_s=poll_s)
    try:
        c.core.patch_namespaced_pod(name, namespace, body)
    except ApiException as exc:
        if exc.status == 422 and current is None:
            return _recreate_pod(c, namespace, name, body, poll_s=poll_s)
        raise
    return "updated"


def wait_for_ready(c: Clients, namespace: str, slug: str, *, timeout_s: int = 240, poll_s: int = 5) -> bool:
    """Lê o `Pod` direto (`read_namespaced_pod`, não `..._pod_status`: o
    RBAC continua precisando só de `get` em `pods`) e considera pronto
    quando `status.phase == "Running"` E o container `kirocrew` está
    `ready`. Sidecars de extensão ficam de fora de propósito: a
    readiness do Pod é global, e um sidecar que ainda espera ação do
    dev (ex.: login SSO) não pode travar o acesso ao dashboard."""
    deadline = time.time() + timeout_s
    name = f"kirocrew-{slug}"
    while time.time() < deadline:
        pod = c.core.read_namespaced_pod(name, namespace)
        status = pod.status
        main = [cs for cs in (status.container_statuses or []) if cs.name == "kirocrew"]
        if status.phase == "Running" and main and main[0].ready:
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
    automático por inatividade continua pendente, ver README, seção
    "Known limitations").

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


def teardown_ext_resources(c: Clients, namespace: str, slug: str) -> str:
    """Remove o ConfigMap de arquivos das extensões (junto do workload).
    O Secret `krewhub-ext-<slug>` NUNCA é deletado aqui (o RBAC não tem
    `delete` em secrets) -- suas chaves são apagadas por
    `wipe_ext_secret_keys`."""
    return _delete_ignore_not_found(
        c.core.delete_namespaced_config_map,
        files_configmap_name(slug),
        namespace,
        resource="ConfigMap",
    )


def reconcile_dev(settings: Settings, owner_id: str, ext_plans: tuple[ExtPlan, ...] = ()) -> dict:
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
    contributions = [(p.ext_id, p.contribution) for p in ext_plans]
    steps = {
        "namespace": ensure_dev_namespace(c, namespace),
        "secret": ensure_secret(c, namespace, slug, owner_id),
        "configmap": ensure_configmap(c, namespace, slug, host, settings),
        "pvc": ensure_pvc(c, namespace, slug, settings),
        "service": ensure_service(c, namespace, slug),
        "networkpolicy": ensure_networkpolicy(c, namespace, slug, settings),
    }
    if ext_plans:
        # Pod só depois: ele referencia o Secret e o ConfigMap das extensões.
        steps["ext_secret"] = ensure_ext_secret(
            c, namespace, slug, generated=tuple(k for p in ext_plans for k in p.generated_keys)
        )
        steps["ext_files"] = ensure_ext_files_configmap(
            c, namespace, slug, collect_files(contributions)
        )
    steps["pod"] = ensure_pod(
        c, namespace, slug, settings, body=tpl.build_pod(namespace, slug, settings, contributions)
    )
    logger.info("reconcile owner_id=%s namespace=%s slug=%s steps=%s", owner_id, namespace, slug, steps)
    return {"owner_id": owner_id, "slug": slug, "namespace": namespace, "host": host, "steps": steps}
