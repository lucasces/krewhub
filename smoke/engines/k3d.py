"""Engine `k3d` -- stub estruturado na mesma interface, NÃO suportado
neste host hoje.

`k3d` roda k3s DENTRO de containers Docker/Podman (é literalmente "k3s em
Docker") -- herda exatamente as mesmas duas paredes documentadas em
`kind.py` (mount de `/lib/modules` hardcoded, path fixo de socket do
container runtime), porque o node-container do k3d também espera módulos
de kernel montados de um path clássico e fala com o runtime num socket
que não é o rootless padrão deste host. Mesma decisão: sem
`/lib/modules` clássico presente e sem symlink/patch manual (que o plano
pediu pra reportar antes de aplicar), não dá pra validar `up()` de
verdade aqui -- fica só o stub, pronto pra implementar se o host mudar."""

from __future__ import annotations

import shutil

from .base import ClusterEngine, ClusterHandle, EngineAvailability, EngineError


class K3dEngine(ClusterEngine):
    name = "k3d"

    def is_available(self) -> EngineAvailability:
        binary = shutil.which("k3d")
        if not binary:
            return EngineAvailability(
                ok=False,
                reason=(
                    "binário 'k3d' não está no PATH deste host -- e, como "
                    "k3d roda k3s dentro de containers Docker/Podman, herda "
                    "as mesmas duas paredes estruturais documentadas em "
                    "kind.py (mount de /lib/modules, path do socket do "
                    "runtime) -- ver docstring deste módulo"
                ),
            )
        return EngineAvailability(
            ok=False,
            reason=(
                "'k3d' está instalado, mas as paredes estruturais deste "
                "host (mesmas de kind.py, herdadas por k3d rodar k3s em "
                "containers Docker/Podman) seguem valendo"
            ),
        )

    def up(self) -> ClusterHandle:
        raise EngineError(
            "engine 'k3d' não suportado neste host -- rode is_available() "
            "e veja o motivo, ou escolha outro KREWHUB_SMOKE_K8S_ENGINE"
        )

    def down(self) -> None:
        return None
