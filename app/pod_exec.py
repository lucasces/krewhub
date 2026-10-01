"""`kubectl exec` genérico sobre o Pod de um dev -- extraído de
`kiro_login.py`/`session_client.py` (que tinham cada um a sua cópia de
`_find_kirocrew_pod` e do `stream(...)` com `container="kirocrew"` fixo)
pra que qualquer container do Pod (o `kirocrew` ou o sidecar de uma
extensão) seja alvo do mesmo mecanismo.

`DetachedFlow`/`run_detached` generalizam a técnica do device-flow do
`kiro-cli login` (ver docstring de `app/kiro_login.py` pro porquê de pty):
um script Python DENTRO do container cria um pty próprio e lê stdin de
uma FIFO, `setsid`-destacado da sessão do exec pra sobreviver à
desconexão. O "roteiro" de prompts e os marcadores de fim viram
parâmetros; nada aqui conhece `kiro-cli` nem `aws-sso`."""

from __future__ import annotations

import base64
import re
import shlex
import time
from dataclasses import dataclass

from kubernetes.stream import stream

from app.k8s_manager import Clients
from app.k8s_templates import OWNER_LABEL_KEY

MAIN_CONTAINER = "kirocrew"

_TAG_RE = re.compile(r"^[a-z0-9_]+$")

_DRIVER_SCRIPT = """import os, pty, sys

FIFO = sys.argv[1]
if not os.path.exists(FIFO):
    os.mkfifo(FIFO)
fd = os.open(FIFO, os.O_RDWR)
os.dup2(fd, 0)

pty.spawn(sys.argv[2:])
"""
_DRIVER_B64 = base64.b64encode(_DRIVER_SCRIPT.encode()).decode()


class PodExecError(RuntimeError):
    pass


def find_dev_pod(
    c: Clients, namespace: str, slug: str, *, error_cls: type[Exception] = PodExecError
) -> str:
    """Namespace é COMPARTILHADO entre devs -- o selector precisa incluir
    o slug, senão "app=kirocrew" sozinho casaria com o pod de QUALQUER
    outro dev. `error_cls` deixa cada chamador manter o tipo de erro do
    seu domínio (`SessionError`, `KiroLoginError`, ...)."""
    selector = f"app=kirocrew,{OWNER_LABEL_KEY}={slug}"
    pods = c.core.list_namespaced_pod(namespace, label_selector=selector)
    running = [p for p in pods.items if p.status.phase == "Running"]
    if not running:
        raise error_cls(f"nenhum pod 'kirocrew' Running em {namespace} pro slug={slug!r}")
    return running[0].metadata.name


def exec_command(
    c: Clients, pod_name: str, namespace: str, command: list[str], *, container: str = MAIN_CONTAINER
) -> str:
    return stream(
        c.core.connect_get_namespaced_pod_exec,
        pod_name,
        namespace,
        container=container,
        command=command,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


def exec_sh(
    c: Clients, pod_name: str, namespace: str, script: str, *, container: str = MAIN_CONTAINER
) -> str:
    return exec_command(c, pod_name, namespace, ["sh", "-c", script], container=container)


@dataclass(frozen=True)
class DetachedFlow:
    """Roteiro de um processo interativo destacado dentro de um container.

    - `command`: argv do processo (roda sob um pty).
    - `tag`: isola os arquivos em /tmp (`<tag>_driver.py`, `<tag>_fifo`,
      `<tag>_log`) -- só `[a-z0-9_]`.
    - `script`: `[(esperar_por, enviar), ...]` -- pra cada par, espera o
      texto aparecer no log e escreve `enviar` na stdin do processo.
    - `done_markers`: o fluxo termina quando TODOS aparecem no log.

    O container precisa de `python3`, `base64` e de /tmp gravável."""

    container: str
    command: tuple[str, ...]
    tag: str
    script: tuple[tuple[str, str], ...] = ()
    done_markers: tuple[str, ...] = ()
    stage_timeout: float = 15.0
    poll_interval: float = 0.5

    def __post_init__(self) -> None:
        if not _TAG_RE.match(self.tag):
            raise ValueError(f"tag inválida: {self.tag!r} (use só [a-z0-9_])")

    @property
    def driver_path(self) -> str:
        return f"/tmp/{self.tag}_driver.py"

    @property
    def fifo_path(self) -> str:
        return f"/tmp/{self.tag}_fifo"

    @property
    def log_path(self) -> str:
        return f"/tmp/{self.tag}_log"


def read_log(c: Clients, pod_name: str, namespace: str, flow: DetachedFlow) -> str:
    return exec_sh(
        c, pod_name, namespace, f"cat {flow.log_path} 2>/dev/null; true", container=flow.container
    )


def _wait_for(
    c: Clients, pod_name: str, namespace: str, flow: DetachedFlow, needles: tuple[str, ...]
) -> str:
    deadline = time.monotonic() + flow.stage_timeout
    log = ""
    while time.monotonic() < deadline:
        log = read_log(c, pod_name, namespace, flow)
        if all(n in log for n in needles):
            return log
        time.sleep(flow.poll_interval)
    raise PodExecError(
        f"timeout ({flow.stage_timeout}s) esperando {list(needles)!r} no log de {flow.tag!r}. "
        f"log atual: {log!r}"
    )


def _send(c: Clients, pod_name: str, namespace: str, flow: DetachedFlow, text: str) -> None:
    payload = base64.b64encode(text.encode()).decode()
    exec_sh(
        c,
        pod_name,
        namespace,
        f"echo {payload} | base64 -d | tee {flow.fifo_path} > /dev/null",
        container=flow.container,
    )


def run_detached(c: Clients, pod_name: str, namespace: str, flow: DetachedFlow) -> str:
    """Dispara `flow.command` destacado, percorre o roteiro e devolve o
    log assim que todos os `done_markers` aparecerem. Fire-and-forget: o
    processo continua rodando no container depois que esta função
    retorna (ex.: polling de um device-flow esperando o clique do
    usuário). Levanta `PodExecError` em timeout de qualquer estágio."""
    exec_sh(
        c,
        pod_name,
        namespace,
        f"echo {_DRIVER_B64} | base64 -d | tee {flow.driver_path} > /dev/null",
        container=flow.container,
    )
    launch = (
        f"rm -f {flow.fifo_path} {flow.log_path}; "
        f"setsid python3 {flow.driver_path} {flow.fifo_path} {shlex.join(flow.command)} "
        f"> {flow.log_path} 2>&1 < /dev/null &"
    )
    exec_sh(c, pod_name, namespace, launch, container=flow.container)

    for needle, send in flow.script:
        _wait_for(c, pod_name, namespace, flow, (needle,))
        _send(c, pod_name, namespace, flow, send)

    return _wait_for(c, pod_name, namespace, flow, flow.done_markers)
