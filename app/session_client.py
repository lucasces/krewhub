"""Emissão do token de sessão do dashboard (`kirocrew token --ttl ...`) --
automatiza o que até agora era manual: `kubectl exec ... kirocrew token`
+ colar a URL à mão.

Diferente do `kiro-cli login` (device-flow, exige pty/wizard interativo --
ver `oidc-client-poc`/README do GitOps), `kirocrew token` é um comando
não-interativo: imprime a URL com `?token=...` e sai. `exec` simples
(sem pty, sem FIFO) é suficiente aqui -- confirmado ao vivo.

Nunca persiste o token em si -- é uma credencial de sessão (URL com
`?token=` embutido). Só a URL é retornada na resposta HTTP; o que fica em
SQLite (store.py) é só o timestamp de quando foi emitido."""

from __future__ import annotations

import re
import urllib.parse

from app import pod_exec
from app.k8s_manager import Clients

_TOKEN_URL_RE = re.compile(r"https?://\S+\?token=\S+")


class SessionError(RuntimeError):
    pass


def _find_kirocrew_pod(c: Clients, namespace: str, slug: str) -> str:
    return pod_exec.find_dev_pod(c, namespace, slug, error_cls=SessionError)


def issue_token_url(
    c: Clients,
    *,
    namespace: str,
    slug: str,
    host: str,
    public_port: str,
    scheme: str = "http",
    ttl: str = "24h",
) -> str:
    """Roda `kirocrew token --ttl {ttl}` dentro do pod e devolve a URL
    pública correta (host/porta do CHP, não `localhost:5476` interno que
    o comando imprime por padrão -- só o `?token=...` é reaproveitado)."""
    pod_name = _find_kirocrew_pod(c, namespace, slug)
    raw = pod_exec.exec_command(c, pod_name, namespace, ["kirocrew", "token", "--ttl", ttl])
    match = _TOKEN_URL_RE.search(raw)
    if not match:
        raise SessionError(f"não consegui extrair a URL com token da saída: {raw!r}")

    internal_url = match.group(0)
    token = urllib.parse.parse_qs(urllib.parse.urlsplit(internal_url).query).get("token")
    if not token:
        raise SessionError(f"URL sem query param 'token': {internal_url!r}")

    return f"{scheme}://{host}:{public_port}/?token={urllib.parse.quote(token[0], safe='')}"


def revoke_session(c: Clients, *, namespace: str, slug: str) -> str:
    """Executa `kirocrew logout` dentro do pod `kirocrew-{slug}` --
    revoga de verdade TODAS as sessões ativas do dashboard desse dev
    (mecanismo documentado em docs/ARCHITECTURE.md, seção "`/close` vs
    `/logout`": `kirocrew logout` faz um
    `POST http://127.0.0.1:<port>/api/logout` LOCAL ao pod, autenticado
    com o mesmo `X-Local-Secret` de arquivo que `kirocrew token` já usa
    -- não precisa de pty/wizard, `exec` simples como `issue_token_url`
    acima). O servidor bump a um contador de geração PERSISTIDO que
    tanto o cookie de acesso (`mc_token_*`) quanto o de refresh
    (`mc_refresh_*`) checam na validação -- então cookies já emitidos
    (mesmo os que o navegador ainda tiver guardado) passam a ser
    rejeitados no próximo request, sem precisar tocar no navegador.
    NÃO reinicia o processo nem o pod -- workspace/memória/chat/o resto
    do estado do dev continuam intactos.

    Levanta SessionError se a saída não confirmar sucesso explícito
    (nunca finge sucesso silenciosamente) -- cobre tanto "gateway não
    está rodando" quanto qualquer outra falha que `kirocrew logout`
    reporte."""
    pod_name = _find_kirocrew_pod(c, namespace, slug)
    raw = pod_exec.exec_command(c, pod_name, namespace, ["kirocrew", "logout"])
    if "✅" not in raw:  # único marcador de sucesso que `kiro_crew.cli_server._logout` imprime
        raise SessionError(f"kirocrew logout não confirmou sucesso: {raw!r}")
    return raw.strip()
