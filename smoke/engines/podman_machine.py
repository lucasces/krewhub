"""Engine `podman-machine` -- o único engine funcional neste host hoje
(default de fato quando nenhum outro é explicitamente pedido e disponível
-- mas nunca escolhido silenciosamente, ver `run_smoke.py`).

Por que este e não kind/k3d: kind e k3d rodam "nodes" Kubernetes DENTRO
de containers do runtime local (Docker/Podman), e batem em duas paredes
estruturais deste host NixOS -- ver `kind.py`/`k3d.py` pra evidência.
`podman machine` sobe uma VM real (QEMU acelerado por KVM -- `/dev/kvm`
confirmado disponível neste host) com um kernel Fedora CoreOS de verdade,
que TEM `/lib/modules` clássico e roda um container runtime normal lá
dentro -- as duas paredes simplesmente não existem nesse contexto. Dentro
dessa VM instalamos k3s NATIVAMENTE (não em mais um container aninhado)
via o instalador oficial (`get.k3s.io`) -- um único binário que já traz
containerd embutido e o controlador de NetworkPolicy do kube-router
habilitado por padrão (crítico pro smoke-test de isolamento entre pods de
dev, que é o teste mais importante desta camada).

Pré-requisito descoberto ao vivo (não documentado antes): a imagem padrão
do `podman machine` desta versão (5.8.6, empacotada pelo Nix) NÃO traz
`qemu-img`/`qemu-system-x86_64`, `gvproxy` nem `virtiofsd` embutidos no
`$PATH` nem em `libexec/podman` (diferente de instalações via pacote
distro tradicional, que normalmente empacotam esses helpers junto). Este
engine resolve os três via `nix build nixpkgs#<pkg> --no-link
--print-out-paths` (rápido e cacheado depois da primeira vez) e escreve
`~/.config/containers/containers.conf` com `helper_binaries_dir`
apontando pra eles -- sem isso, `podman machine start` falha com erro
explícito (`could not find "gvproxy"...`/`failed to find virtiofsd`).

Fluxo de `up()`:
  1. resolve os 3 helper binaries via nix, garante containers.conf.
  2. cria a VM (`podman machine init`) se não existir; inicia se parada.
  3. instala k3s dentro da VM via SSH (`podman machine ssh`) se ainda não
     instalado; sobe o systemd unit; espera o node ficar Ready.
  4. busca o kubeconfig de dentro da VM, reescreve `server:` pra um
     túnel SSH local (`ssh -L <porta>:127.0.0.1:6443 ...`) que ESTE
     processo mantém aberto em background -- sem isso, o kubeconfig
     aponta pra `127.0.0.1:6443` de dentro da VM, inalcançável do host.
  5. devolve um `ClusterHandle` usável imediatamente por
     `kubectl`/client Python `kubernetes` rodando no HOST -- inclusive
     `exec` (usado por `chp_client.py`/`session_client.py`) e
     `port-forward` (usado pro passo final do smoke-test, mesmo padrão
     já usado contra o cluster de produção), ambos confirmados ao vivo.

`down()` por padrão remove a VM inteira (`podman machine rm -f`) --
"efêmero" de verdade. Setar `KREWHUB_SMOKE_KEEP_MACHINE=1` pula a
remoção (só para/mata o túnel) pra iteração rápida repetida sem pagar de
novo o custo de download de imagem + instalação do k3s (~2-3min)."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time

from .base import ClusterEngine, ClusterHandle, EngineAvailability, EngineError

_MACHINE_NAME = os.environ.get("KREWHUB_SMOKE_PODMAN_MACHINE_NAME", "krewhub-smoke")
_LOCAL_API_PORT = int(os.environ.get("KREWHUB_SMOKE_LOCAL_API_PORT", "16443"))
_KUBECONFIG_PATH = os.environ.get(
    "KREWHUB_SMOKE_KUBECONFIG_PATH", "/tmp/krewhub-smoke-kubeconfig.yaml"
)
_CONTEXT_NAME = "krewhub-smoke"
_CONTAINERS_CONF = os.path.expanduser("~/.config/containers/containers.conf")

_HELPER_PACKAGES = ["qemu", "gvproxy", "virtiofsd"]


def _run(cmd: list[str], *, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if check:
            raise EngineError(f"comando expirou ({' '.join(cmd)}, timeout={timeout}s)") from exc
        # check=False -- chamador tratou como melhor esforço (ex.: down()
        # não pode travar o script por causa de um 'machine stop' lento);
        # devolve um CompletedProcess sintético em vez de propagar.
        return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr=str(exc))
    if check and result.returncode != 0:
        raise EngineError(
            f"comando falhou ({' '.join(cmd)}): rc={result.returncode} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return result


def _resolve_nix_bin_dirs() -> list[str]:
    """Resolve os 3 helper packages via `nix build`, cacheado -- rápido
    depois da primeira vez. Levanta EngineError com mensagem acionável se
    `nix` não existir (não finge sucesso)."""
    if not shutil.which("nix"):
        raise EngineError(
            "'nix' não está no PATH -- este engine resolve qemu-img/"
            "qemu-system-x86_64/gvproxy/virtiofsd via 'nix build nixpkgs#<pkg>' "
            "porque a imagem do podman machine deste host não os traz "
            "embutidos; sem 'nix', instale-os pelo gerenciador de pacotes do "
            "sistema e aponte manualmente helper_binaries_dir em "
            f"{_CONTAINERS_CONF}"
        )
    dirs = []
    for pkg in _HELPER_PACKAGES:
        result = _run(
            ["nix", "build", f"nixpkgs#{pkg}", "--no-link", "--print-out-paths"],
            timeout=300,
        )
        store_path = result.stdout.strip().splitlines()[-1]
        dirs.append(f"{store_path}/bin")
    return dirs


def _ensure_containers_conf(bin_dirs: list[str]) -> None:
    os.makedirs(os.path.dirname(_CONTAINERS_CONF), exist_ok=True)
    dirs_toml = ", ".join(f'"{d}"' for d in bin_dirs)
    content = f"[engine]\nhelper_binaries_dir = [{dirs_toml}]\n"
    existing = ""
    if os.path.isfile(_CONTAINERS_CONF):
        with open(_CONTAINERS_CONF) as f:
            existing = f.read()
    if existing.strip() != content.strip():
        with open(_CONTAINERS_CONF, "w") as f:
            f.write(content)


def _machine_status() -> str | None:
    """None se a VM não existe; senão o texto da coluna LAST UP/STATE."""
    result = _run(["podman", "machine", "list", "--format", "{{.Name}}\t{{.Running}}"], check=False)
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0].rstrip("*") == _MACHINE_NAME:
            return "running" if parts[1].strip() == "true" else "stopped"
    return None


def _ssh(cmd: str, *, timeout: int = 60) -> str:
    result = _run(["podman", "machine", "ssh", _MACHINE_NAME, "--", cmd], timeout=timeout)
    return result.stdout


def _identity_and_port() -> tuple[str, int]:
    result = _run(
        [
            "podman",
            "machine",
            "inspect",
            _MACHINE_NAME,
            "--format",
            "{{.SSHConfig.Port}}|{{.SSHConfig.IdentityPath}}",
        ]
    )
    port_str, identity = result.stdout.strip().split("|", 1)
    return identity, int(port_str)


class PodmanMachineEngine(ClusterEngine):
    name = "podman-machine"

    def __init__(self) -> None:
        self._tunnel_proc: subprocess.Popen | None = None
        self._created_machine = False

    def is_available(self) -> EngineAvailability:
        if not shutil.which("podman"):
            return EngineAvailability(ok=False, reason="binário 'podman' não está no PATH")
        if not os.path.exists("/dev/kvm"):
            return EngineAvailability(
                ok=False,
                reason="/dev/kvm não existe -- sem aceleração de virtualização, "
                "'podman machine' (QEMU) não é viável (rodaria em emulação pura, "
                "inviável pra um smoke-test)",
            )
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            return EngineAvailability(ok=False, reason="/dev/kvm existe mas sem permissão de leitura/escrita pro usuário atual")
        if not shutil.which("nix"):
            return EngineAvailability(
                ok=False,
                reason="'nix' não está no PATH -- necessário pra resolver "
                "qemu-img/gvproxy/virtiofsd (ver docstring do módulo); sem eles "
                "'podman machine start' falha",
            )
        return EngineAvailability(
            ok=True, reason="podman + /dev/kvm (r/w) + nix confirmados"
        )

    def up(self) -> ClusterHandle:
        availability = self.is_available()
        if not availability.ok:
            raise EngineError(f"engine 'podman-machine' indisponível: {availability.reason}")

        bin_dirs = _resolve_nix_bin_dirs()
        _ensure_containers_conf(bin_dirs)

        status = _machine_status()
        if status is None:
            _run(
                [
                    "podman",
                    "machine",
                    "init",
                    "--cpus",
                    "4",
                    "--memory",
                    "4096",
                    "--disk-size",
                    "20",
                    _MACHINE_NAME,
                ],
                timeout=600,
            )
            self._created_machine = True
            status = "stopped"
        if status == "stopped":
            _run(["podman", "machine", "start", _MACHINE_NAME], timeout=180)

        self._ensure_k3s_ready()
        self._write_kubeconfig()
        self._open_tunnel()

        return ClusterHandle(
            kubeconfig_path=_KUBECONFIG_PATH, context=_CONTEXT_NAME, node_ip=None
        )

    def _ensure_k3s_ready(self) -> None:
        has_k3s = _ssh("which k3s || true").strip()
        if not has_k3s:
            _ssh(
                "curl -sfL https://get.k3s.io | "
                "sh -s - --write-kubeconfig-mode 644 --disable traefik --disable servicelb",
                timeout=300,
            )
        _ssh("sudo systemctl enable --now k3s", timeout=60)

        deadline = time.time() + 120
        while time.time() < deadline:
            out = _ssh("sudo k3s kubectl get nodes --no-headers 2>/dev/null || true")
            if "Ready" in out:
                return
            time.sleep(3)
        raise EngineError("node k3s não ficou Ready dentro do timeout (120s)")

    def _write_kubeconfig(self) -> None:
        raw = _ssh("cat /etc/rancher/k3s/k3s.yaml")
        rewritten = (
            raw.replace("server: https://127.0.0.1:6443", f"server: https://127.0.0.1:{_LOCAL_API_PORT}")
            .replace("name: default", f"name: {_CONTEXT_NAME}")
            .replace("cluster: default", f"cluster: {_CONTEXT_NAME}")
            .replace("user: default", f"user: {_CONTEXT_NAME}")
            .replace("current-context: default", f"current-context: {_CONTEXT_NAME}")
        )
        with open(_KUBECONFIG_PATH, "w") as f:
            f.write(rewritten)

    def _open_tunnel(self) -> None:
        # já tem um túnel nosso vivo nessa porta? reaproveita (idempotência
        # entre chamadas de up() na mesma sessão de processo).
        check = subprocess.run(
            ["bash", "-c", f"ss -tln 2>/dev/null | grep -q ':{_LOCAL_API_PORT} '"],
            capture_output=True,
        )
        if check.returncode == 0:
            return
        identity, ssh_port = _identity_and_port()
        self._tunnel_proc = subprocess.Popen(
            [
                "ssh",
                "-i",
                identity,
                "-p",
                str(ssh_port),
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ExitOnForwardFailure=yes",
                "-L",
                f"{_LOCAL_API_PORT}:127.0.0.1:6443",
                "core@localhost",
                "-N",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            check = subprocess.run(
                ["bash", "-c", f"ss -tln 2>/dev/null | grep -q ':{_LOCAL_API_PORT} '"],
                capture_output=True,
            )
            if check.returncode == 0:
                return
            time.sleep(0.5)
        raise EngineError(f"túnel SSH pra porta {_LOCAL_API_PORT} não abriu a tempo")

    def load_image(self, build_dir: str, tag: str) -> str:
        """Builda com o podman DE DENTRO da VM (Fedora CoreOS já traz
        podman) e importa o resultado no containerd embutido do k3s via
        `ctr images import` -- validado ao vivo nesta fatia. Devolve
        `localhost/<tag>` (prefixo que `podman save`/`ctr import` usam
        pra imagens sem registry explícito), pronta pra `image:` no
        manifest com `imagePullPolicy: IfNotPresent` (default quando a
        tag não é `latest`) -- sem isso o k3s tentaria puxar de um
        registry real e falharia (a imagem só existe local)."""
        remote_dir = f"/tmp/krewhub-smoke-image-{tag}"
        _ssh(f"rm -rf {remote_dir} && mkdir -p {remote_dir}")
        identity, ssh_port = _identity_and_port()
        tar_proc = subprocess.run(
            ["tar", "-C", build_dir, "-cf", "-", "."],
            capture_output=True,
        )
        if tar_proc.returncode != 0:
            raise EngineError(f"tar do build_dir falhou: {tar_proc.stderr!r}")
        untar = subprocess.run(
            [
                "ssh",
                "-i",
                identity,
                "-p",
                str(ssh_port),
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "core@localhost",
                f"tar -C {remote_dir} -xf -",
            ],
            input=tar_proc.stdout,
            capture_output=True,
        )
        if untar.returncode != 0:
            raise EngineError(f"envio do build context pra VM falhou: {untar.stderr!r}")

        image_ref = f"localhost/{tag}"
        _ssh(f"cd {remote_dir} && podman build -t {tag} .", timeout=300)
        tar_path = f"/tmp/{tag.replace(':', '_')}.tar"
        _ssh(f"podman save {image_ref} -o {tar_path}", timeout=120)
        _ssh(f"sudo k3s ctr images import {tar_path}", timeout=120)
        return image_ref

    def down(self) -> None:
        if self._tunnel_proc is not None:
            try:
                os.killpg(os.getpgid(self._tunnel_proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            self._tunnel_proc = None

        if os.environ.get("KREWHUB_SMOKE_KEEP_MACHINE") == "1":
            _run(["podman", "machine", "stop", _MACHINE_NAME], check=False, timeout=120)
            return

        _run(["podman", "machine", "rm", "-f", _MACHINE_NAME], check=False, timeout=120)
        if os.path.isfile(_KUBECONFIG_PATH):
            os.remove(_KUBECONFIG_PATH)
