"""Registra rota no configurable-http-proxy via API REST -- reaproveita a
função (era um passo manual: `kubectl exec` no pod do CHP + curl pra
localhost:8001).

Por quê exec e não HTTP direto: a API de admin do CHP (porta 8001) é
`--api-ip 127.0.0.1` DE PROPÓSITO (ver o manifest do CHP no repo GitOps
do homelab, fora deste repositório) -- não tem Service
nem NetworkPolicy abrindo essa porta pra fora do pod. O comentário lá já
previa isso: "Quando o KrewHub central existir, ele roda DENTRO deste
mesmo pod (sidecar) ou ganha uma rota de rede explícita revisada nesse
momento". Nesta fatia (protótipo local, fora do cluster) a forma de
respeitar essa arquitetura sem reabrir a porta de admin pra rede nenhuma é
seguir exatamente o mesmo caminho que era manual: `exec` no pod do CHP e
falar com localhost de dentro dele -- o token de auth (CONFIGPROXY_AUTH_TOKEN)
nunca sai do pod nem passa pela rede."""

from __future__ import annotations

import json
import logging

from app import pod_exec
from app.k8s_manager import Clients

logger = logging.getLogger("krewhub.chp")


class CHPError(RuntimeError):
    pass


def _find_chp_pod(c: Clients, chp_namespace: str, pod_label: str) -> str:
    pods = c.core.list_namespaced_pod(chp_namespace, label_selector=pod_label)
    if not pods.items:
        raise CHPError(f"nenhum pod do CHP em ns={chp_namespace} label={pod_label}")
    return pods.items[0].metadata.name


def _exec_curl(c: Clients, chp_namespace: str, pod_name: str, curl_cmd: str) -> str:
    return pod_exec.exec_command(c, pod_name, chp_namespace, ["sh", "-c", curl_cmd], container=None)


def self_target(settings) -> str:
    """Target da própria rota do krewhub-central no CHP -- o mesmo valor
    usado no auto-registro de startup (ver app/main.py). Centralizado
    aqui pra que o reconcile periódico (app/reconcile.py) re-registre a
    rota EXATAMENTE com o target que o startup usaria, sem divergir."""
    return (
        f"http://krewhub-central.{settings.chp_namespace}"
        f".svc.cluster.local:{settings.self_port}"
    )


# Porta em que o container kirocrew de cada pod-por-dev escuta -- é a
# mesma fixada no Service/NetworkPolicy por-dev (ver app/k8s_templates.py)
# e no registro de rota de /provision (ver app/main.py::_do_provision).
# Centralizada aqui pra que o reconcile periódico monte o MESMO target.
DEV_POD_PORT = 5476


def dev_target(slug: str, namespace: str) -> str:
    """Target da rota de um pod-por-dev no CHP -- `kirocrew-<slug>` no
    namespace compartilhado dos devs, na porta do dashboard. Igual ao
    que /provision registra, pra que o reconcile re-registre sem
    divergir."""
    return f"http://kirocrew-{slug}.{namespace}.svc.cluster.local:{DEV_POD_PORT}"


def register_route(c: Clients, settings, *, host: str, target: str) -> dict:
    """POST /api/routes/{host}/ {"target": target} -- idempotente por
    natureza na própria API do CHP (recriar a mesma rota substitui)."""
    pod_name = _find_chp_pod(c, settings.chp_namespace, settings.chp_pod_label)
    payload = json.dumps({"target": target})
    curl_cmd = (
        f"curl -s -X POST "
        f'-H "Authorization: token $CONFIGPROXY_AUTH_TOKEN" '
        f'-H "Content-Type: application/json" '
        f"--data '{payload}' "
        f'-w "\\nHTTP_STATUS:%{{http_code}}\\n" '
        f"http://localhost:{settings.chp_admin_port}/api/routes/{host}/"
    )
    raw = _exec_curl(c, settings.chp_namespace, pod_name, curl_cmd)
    status = _parse_status(raw)
    if status not in (200, 201):
        raise CHPError(f"registro de rota falhou (status={status}): {raw!r}")
    logger.info("rota registrada host=%s target=%s status=%s", host, target, status)
    return {"host": host, "target": target, "status": status}


def list_routes(c: Clients, settings) -> dict[str, dict]:
    """GET /api/routes -- devolve a tabela de rotas atual do CHP, dict
    host -> metadados da rota (`{"target": ..., ...}`). É esta tabela
    que fica VAZIA quando o pod do CHP reinicia (as rotas vivem só em
    memória), e é o que o reconcile periódico (app/reconcile.py) compara
    contra o estado persistido pra detectar e corrigir a deriva.

    Mesma via do register_route: `exec` + curl contra `localhost:8001`
    DENTRO do pod do CHP (a API admin só escuta em 127.0.0.1, ver a
    docstring do módulo). O corpo (JSON) é separado do `HTTP_STATUS`
    anexado via `-w`, pra distinguir "lista vazia de rotas" (200, `{}`)
    de uma falha do curl/API."""
    pod_name = _find_chp_pod(c, settings.chp_namespace, settings.chp_pod_label)
    curl_cmd = (
        f"curl -s "
        f'-H "Authorization: token $CONFIGPROXY_AUTH_TOKEN" '
        f'-w "\\nHTTP_STATUS:%{{http_code}}\\n" '
        f"http://localhost:{settings.chp_admin_port}/api/routes"
    )
    raw = _exec_curl(c, settings.chp_namespace, pod_name, curl_cmd)
    status = _parse_status(raw)
    if status != 200:
        raise CHPError(f"listagem de rotas falhou (status={status}): {raw!r}")
    body = "\n".join(
        line for line in raw.splitlines() if not line.startswith("HTTP_STATUS:")
    ).strip()
    if not body:
        return {}
    try:
        routes = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CHPError(f"corpo da listagem de rotas não é JSON válido: {body!r}") from exc
    if not isinstance(routes, dict):
        raise CHPError(f"listagem de rotas não é um objeto JSON: {routes!r}")
    return routes


def _parse_status(raw: str) -> int:
    for line in raw.splitlines():
        if line.startswith("HTTP_STATUS:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    raise CHPError(f"não consegui extrair HTTP_STATUS da resposta do exec: {raw!r}")
