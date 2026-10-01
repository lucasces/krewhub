"""Automação do `kiro-cli login` (device-flow) -- elimina o `kubectl exec`
manual + a técnica pty+FIFO que até esta fatia era feita à mão
(documentada em detalhe no README do repo GitOps do homelab, fora deste
repositório).

Cobre os dois caminhos já vistos manualmente:

- `mode="org"`  -> `kiro-cli login --use-device-flow --license pro
  --identity-provider <start-url> --region <region>` (Identity Center
  corporativo). Pede confirmação (Enter) de dois prompts pré-preenchidos:
  Start URL, depois Region.
- `mode="personal"` -> `kiro-cli login --use-device-flow` (sem
  `--license`/`--identity-provider`/`--region`). Mostra um menu de seleção
  (Builder ID / Google / GitHub / Your Organization) com "Use with
  Builder ID" já destacado por default -- confirmado ao vivo que um Enter
  aceita esse default. Builder ID é o login "pessoal" (free) -- não
  tentamos oferecer Google/GitHub aqui porque exigiriam navegar o menu
  (mais Enters/setas), fora do escopo desta fatia.

Em ambos os casos o resultado final é o mesmo formato de saída
(`Code: XXXX-XXXX` / `Open this URL: <url>`), então o parsing é
compartilhado.

Por que precisa de pty: em qualquer um dos dois modos, o wizard do
`kiro-cli` espera confirmação interativa mesmo com as flags relevantes já
preenchidas -- com stdout sem terminal (`kubectl exec` comum) ele detecta
a ausência de TTY e derruba o valor da flag (ex.:
`region: must be a valid host label`) ou nem chega a mostrar o menu. A
saída: um script Python rodado DENTRO do pod cria seu próprio pty real
(`pty.spawn`) e lê stdin de uma FIFO (aberta em modo RDWR pra não
bloquear o lançamento), tudo `setsid`-destacado da sessão do nosso `exec`
pra sobreviver à desconexão enquanto o processo faz polling do
device-flow.

Fire-and-forget por design: esta função NÃO espera o dev clicar no link.
Ela só dispara o processo, confirma os prompts necessários pro modo
escolhido, e devolve a URL+código assim que aparecerem no log -- o
polling continua rodando no pod depois que a função retorna."""

from __future__ import annotations

import re

from app import pod_exec
from app.k8s_manager import Clients

MODES = ("org", "personal")

_CODE_RE = re.compile(r"Code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})")
_URL_RE = re.compile(r"Open this URL:\s*(\S+)")

_STAGE_TIMEOUT = 15.0


class KiroLoginError(RuntimeError):
    pass


def _find_kirocrew_pod(c: Clients, namespace: str, slug: str) -> str:
    return pod_exec.find_dev_pod(c, namespace, slug, error_cls=KiroLoginError)


def whoami(c: Clients, *, namespace: str, slug: str) -> tuple[bool, str]:
    """`kiro-cli whoami` -- não-interativo, não precisa de pty. Usado pra
    idempotência: se já tem sessão válida, não dispara device-flow novo,
    em nenhum dos dois modos."""
    pod_name = _find_kirocrew_pod(c, namespace, slug)
    out = pod_exec.exec_sh(c, pod_name, namespace, "kiro-cli whoami 2>&1; true")
    logged_in = "not logged in" not in out.lower()
    return logged_in, out.strip()


def start_device_flow(
    c: Clients,
    *,
    namespace: str,
    slug: str,
    mode: str,
    identity_provider: str | None = None,
    region: str | None = None,
) -> dict:
    """Dispara o device-flow dentro do pod `kirocrew-{slug}` de
    `namespace` (namespace compartilhado -- `slug` é o que identifica o
    pod DESTE dev entre todos os outros que vivem no mesmo namespace).

    `mode` já deve ter sido validado pelo chamador (só "org"/"personal"
    aceitos aqui -- ver `MODES`). Pra `mode="org"`, `identity_provider` e
    `region` são obrigatórios (resolução de default/env var é
    responsabilidade do chamador, não desta função -- aqui não há
    fallback implícito).

    Idempotente: se já há sessão (`kiro-cli whoami` != "Not logged in"),
    não dispara nada novo, só reporta o estado atual."""
    if mode not in MODES:
        raise KiroLoginError(f"mode inválido: {mode!r} (esperado um de {MODES})")
    if mode == "org" and (not identity_provider or not region):
        raise KiroLoginError(
            "mode='org' exige identity_provider e region resolvidos -- chamador não passou"
        )

    pod_name = _find_kirocrew_pod(c, namespace, slug)

    already, whoami_detail = whoami(c, namespace=namespace, slug=slug)
    if already:
        return {"already_logged_in": True, "whoami": whoami_detail}

    if mode == "org":
        command = (
            "kiro-cli", "login", "--use-device-flow", "--license", "pro",
            "--identity-provider", identity_provider, "--region", region,
        )
        # Dois prompts pré-preenchidos (Start URL, depois Region) --
        # cada um só precisa de um Enter pra confirmar o default.
        script = (("Enter Start URL", "\r"), ("Enter Region", "\r"))
    else:
        command = ("kiro-cli", "login", "--use-device-flow")
        # Menu de seleção (Builder ID / Google / GitHub / Your
        # Organization) com "Use with Builder ID" já destacado -- um
        # Enter aceita esse default (confirmado ao vivo).
        script = (("Select login method", "\r"),)

    flow = pod_exec.DetachedFlow(
        container=pod_exec.MAIN_CONTAINER,
        command=command,
        tag="kiro_login",
        script=script,
        done_markers=("Open this URL",),
        stage_timeout=_STAGE_TIMEOUT,
    )
    try:
        log = pod_exec.run_detached(c, pod_name, namespace, flow)
    except pod_exec.PodExecError as exc:
        raise KiroLoginError(str(exc)) from exc

    code_match = _CODE_RE.search(log)
    url_match = _URL_RE.search(log)
    if not code_match or not url_match:
        raise KiroLoginError(f"não consegui extrair código/URL do device-flow. log: {log!r}")

    # Fire-and-forget: o processo (setsid, destacado) continua fazendo
    # polling do device-flow no pod depois que retornamos -- não esperamos
    # o clique do dev aqui.
    return {
        "already_logged_in": False,
        "verification_url": url_match.group(1),
        "user_code": code_match.group(1),
    }
