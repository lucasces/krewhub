"""Engine `kind` -- implementação real (não mais um stub), funcional em
hosts com Docker "real" (não-rootless) e `/lib/modules` clássico, como
os runners hospedados do GitHub Actions (`ubuntu-latest`): Docker vem
pré-instalado e não-rootless lá, e o path clássico de `/lib/modules`
existe -- as duas paredes estruturais que bloqueiam `kind` neste host de
desenvolvimento (NixOS) simplesmente não existem nesse ambiente.
Validado pelo job `integration-test` de `release.yml` -- que existe
justamente pra provar isso ao vivo em CI, não por suposição -- ver
`AGENTS.md` para o resumo da investigação.

Neste host de desenvolvimento (NixOS, socket do Podman rootless em
`$XDG_RUNTIME_DIR`, sem `/lib/modules` clássico -- módulos ficam em
`/run/current-system/kernel-modules/lib/modules/<versão>`), as duas
paredes estruturais continuam de pé, então `is_available()` retorna
`False` aqui -- mas a checagem é sobre pré-requisitos REAIS (binário +
daemon do Docker acessível + `/lib/modules` presente), não mais um
`False` hardcoded incondicional: qualquer host (como o runner do GitHub
Actions) que satisfaça os pré-requisitos reais passa a `is_available()`
`True` e `up()`/`load_image()`/`down()` funcionam de verdade.

Fluxo de `up()`: `kind create cluster --name ... --kubeconfig ...`
(idempotente -- reaproveita um cluster já rodando com o mesmo nome via
`kind get clusters`, exporta o kubeconfig de novo em vez de recriar).
Contexto é sempre `kind-<nome-do-cluster>` (convenção fixa do próprio
`kind`, não escolhida por este código).

`load_image()`: builda a imagem localmente (`docker build`) e usa `kind
load docker-image` para disponibilizá-la nos nodes do cluster sem
depender de um registry externo -- devolve a própria tag (mesma usada no
`docker build`), pronta pra `image:` no manifest com
`imagePullPolicy: IfNotPresent`."""

from __future__ import annotations

import os
import shutil
import subprocess

from .base import ClusterEngine, ClusterHandle, EngineAvailability, EngineError

_CLUSTER_NAME = os.environ.get("KREWHUB_INTEGRATION_KIND_CLUSTER_NAME", "krewhub-integration")
_CONTEXT_NAME = f"kind-{_CLUSTER_NAME}"
_KUBECONFIG_PATH = os.environ.get(
    "KREWHUB_INTEGRATION_KIND_KUBECONFIG_PATH", "/tmp/krewhub-integration-kind-kubeconfig.yaml"
)


def _run(cmd: list[str], *, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if check:
            raise EngineError(f"comando expirou ({' '.join(cmd)}, timeout={timeout}s)") from exc
        return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr=str(exc))
    if check and result.returncode != 0:
        raise EngineError(
            f"comando falhou ({' '.join(cmd)}): rc={result.returncode} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return result


class KindEngine(ClusterEngine):
    name = "kind"
    # kind bundla local-path-provisioner mas registra a StorageClass
    # resultante como "standard" (default do cluster), não "local-path"
    # como o k3s do engine podman-machine -- confirmado lendo o
    # manifesto que o próprio kind embute
    # (pkg/build/nodeimage/const_storage.go), não por suposição.
    default_storage_class = "standard"

    def is_available(self) -> EngineAvailability:
        if not shutil.which("kind"):
            return EngineAvailability(
                ok=False, reason="binário 'kind' não está no PATH deste host"
            )
        if not shutil.which("docker"):
            return EngineAvailability(
                ok=False,
                reason=(
                    "binário 'docker' não está no PATH -- kind usa o driver "
                    "docker por padrão pra criar os node-containers"
                ),
            )
        docker_check = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=15
        )
        if docker_check.returncode != 0:
            return EngineAvailability(
                ok=False,
                reason=(
                    "'docker info' falhou (daemon não acessível): "
                    f"{docker_check.stderr.strip()[:200]}"
                ),
            )
        if not os.path.isdir("/lib/modules"):
            return EngineAvailability(
                ok=False,
                reason=(
                    "/lib/modules clássico não existe neste host -- kind monta "
                    "esse path (ro) nos node-containers (kindnet/kube-proxy "
                    "esperam módulos de kernel visíveis lá); confirmado ausente "
                    "em hosts NixOS, presente por padrão em ubuntu-latest/"
                    "GitHub Actions"
                ),
            )
        return EngineAvailability(
            ok=True, reason="kind + docker (daemon acessível) + /lib/modules confirmados"
        )

    def up(self) -> ClusterHandle:
        availability = self.is_available()
        if not availability.ok:
            raise EngineError(f"engine 'kind' indisponível: {availability.reason}")

        existing = _run(["kind", "get", "clusters"], check=False)
        if _CLUSTER_NAME not in existing.stdout.split():
            _run(
                [
                    "kind",
                    "create",
                    "cluster",
                    "--name",
                    _CLUSTER_NAME,
                    "--kubeconfig",
                    _KUBECONFIG_PATH,
                    "--wait",
                    "120s",
                ],
                timeout=300,
            )
        else:
            _run(
                [
                    "kind",
                    "export",
                    "kubeconfig",
                    "--name",
                    _CLUSTER_NAME,
                    "--kubeconfig",
                    _KUBECONFIG_PATH,
                ]
            )

        return ClusterHandle(
            kubeconfig_path=_KUBECONFIG_PATH, context=_CONTEXT_NAME, node_ip=None
        )

    def load_image(self, build_dir: str, tag: str) -> str:
        _run(["docker", "build", "-t", tag, build_dir], timeout=300)
        _run(["kind", "load", "docker-image", tag, "--name", _CLUSTER_NAME], timeout=120)
        return tag

    def down(self) -> None:
        _run(["kind", "delete", "cluster", "--name", _CLUSTER_NAME], check=False, timeout=120)
        if os.path.isfile(_KUBECONFIG_PATH):
            os.remove(_KUBECONFIG_PATH)
