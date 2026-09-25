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

import base64
import re
import shlex
import time

from kubernetes.stream import stream

from app.k8s_manager import Clients
from app.k8s_templates import OWNER_LABEL_KEY

MODES = ("org", "personal")

_CODE_RE = re.compile(r"Code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})")
_URL_RE = re.compile(r"Open this URL:\s*(\S+)")

_DRIVER_SCRIPT = """import os, pty, sys

FIFO = "/tmp/kiro_login_stdin_fifo"
if not os.path.exists(FIFO):
    os.mkfifo(FIFO)
fd = os.open(FIFO, os.O_RDWR)
os.dup2(fd, 0)

pty.spawn(sys.argv[1:])
"""
_DRIVER_B64 = base64.b64encode(_DRIVER_SCRIPT.encode()).decode()

_DRIVER_PATH = "/tmp/kiro_login_driver.py"
_FIFO_PATH = "/tmp/kiro_login_stdin_fifo"
_LOG_PATH = "/tmp/kiro_login_out.log"

_POLL_INTERVAL = 0.5
_STAGE_TIMEOUT = 15.0


class KiroLoginError(RuntimeError):
    pass


def _find_kirocrew_pod(c: Clients, namespace: str, slug: str) -> str:
    """Namespace agora é COMPARTILHADO entre devs -- precisa filtrar pelo
    slug do dev, senão "app=kirocrew" sozinho pegaria o pod de qualquer
    outro dev no mesmo namespace."""
    selector = f"app=kirocrew,{OWNER_LABEL_KEY}={slug}"
    pods = c.core.list_namespaced_pod(namespace, label_selector=selector)
    running = [p for p in pods.items if p.status.phase == "Running"]
    if not running:
        raise KiroLoginError(f"nenhum pod 'kirocrew' Running em {namespace} pro slug={slug!r}")
    return running[0].metadata.name


def _exec_sh(c: Clients, pod_name: str, namespace: str, script: str) -> str:
    return stream(
        c.core.connect_get_namespaced_pod_exec,
        pod_name,
        namespace,
        container="kirocrew",
        command=["sh", "-c", script],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


def whoami(c: Clients, *, namespace: str, slug: str) -> tuple[bool, str]:
    """`kiro-cli whoami` -- não-interativo, não precisa de pty. Usado pra
    idempotência: se já tem sessão válida, não dispara device-flow novo,
    em nenhum dos dois modos."""
    pod_name = _find_kirocrew_pod(c, namespace, slug)
    out = _exec_sh(c, pod_name, namespace, "kiro-cli whoami 2>&1; true")
    logged_in = "not logged in" not in out.lower()
    return logged_in, out.strip()


def _wait_for(c: Clients, pod_name: str, namespace: str, needle: str, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    log = ""
    while time.monotonic() < deadline:
        log = _exec_sh(c, pod_name, namespace, f"cat {_LOG_PATH} 2>/dev/null; true")
        if needle in log:
            return log
        time.sleep(_POLL_INTERVAL)
    raise KiroLoginError(
        f"timeout ({timeout}s) esperando {needle!r} no log do device-flow. log atual: {log!r}"
    )


def _send_enter(c: Clients, pod_name: str, namespace: str) -> None:
    _exec_sh(c, pod_name, namespace, f"printf '\\r' | tee {_FIFO_PATH} > /dev/null")


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

    write_script = f"echo {_DRIVER_B64} | base64 -d | tee {_DRIVER_PATH} > /dev/null"
    _exec_sh(c, pod_name, namespace, write_script)

    if mode == "org":
        login_cmd = (
            f"kiro-cli login --use-device-flow --license pro "
            f"--identity-provider {shlex.quote(identity_provider)} "
            f"--region {shlex.quote(region)}"
        )
    else:
        login_cmd = "kiro-cli login --use-device-flow"

    launch_script = (
        f"rm -f {_FIFO_PATH} {_LOG_PATH}; "
        f"setsid python3 {_DRIVER_PATH} {login_cmd} "
        f"> {_LOG_PATH} 2>&1 < /dev/null &"
    )
    _exec_sh(c, pod_name, namespace, launch_script)

    if mode == "org":
        # Dois prompts pré-preenchidos (Start URL, depois Region) --
        # cada um só precisa de um Enter pra confirmar o default.
        _wait_for(c, pod_name, namespace, "Enter Start URL", timeout=_STAGE_TIMEOUT)
        _send_enter(c, pod_name, namespace)
        _wait_for(c, pod_name, namespace, "Enter Region", timeout=_STAGE_TIMEOUT)
        _send_enter(c, pod_name, namespace)
    else:
        # Menu de seleção (Builder ID / Google / GitHub / Your
        # Organization) com "Use with Builder ID" já destacado -- um
        # Enter aceita esse default (confirmado ao vivo).
        _wait_for(c, pod_name, namespace, "Select login method", timeout=_STAGE_TIMEOUT)
        _send_enter(c, pod_name, namespace)

    log = _wait_for(c, pod_name, namespace, "Open this URL", timeout=_STAGE_TIMEOUT)

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
