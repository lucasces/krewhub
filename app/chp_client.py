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

from kubernetes.stream import stream

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
    command = ["sh", "-c", curl_cmd]
    return stream(
        c.core.connect_get_namespaced_pod_exec,
        pod_name,
        chp_namespace,
        command=command,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


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


def _parse_status(raw: str) -> int:
    for line in raw.splitlines():
        if line.startswith("HTTP_STATUS:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    raise CHPError(f"não consegui extrair HTTP_STATUS da resposta do exec: {raw!r}")
