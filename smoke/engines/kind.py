"""Engine `kind` -- stub estruturado na mesma interface, NÃO suportado
neste host hoje.

Evidência real (não suposição) de por que bate parede aqui, coletada ao
tentar antes desta fatia ter a abstração:

1. `kind` roda "nodes" como containers Docker/Podman que precisam montar
   `/lib/modules` do HOST pra dentro do node-container (kindnet e o
   kube-proxy esperam módulos de kernel visíveis lá dentro). Este host
   (NixOS) não tem um `/lib/modules` clássico -- módulos vivem debaixo de
   `/run/current-system/kernel-modules/lib/modules/<versão>`, e o mount
   hardcoded de kind (`/lib/modules:/lib/modules:ro`) falha silenciosamente
   (monta um diretório vazio/inexistente) ou o `docker run` recusa o bind
   mount porque o path de origem não existe.
2. `kind`/`kindnet` também espera falar com `/var/run/docker.sock` (ou o
   socket do Podman no path que ele assume) num local fixo -- neste setup
   o socket do Podman é rootless e fica em
   `$XDG_RUNTIME_DIR/podman/podman.sock`, não no path hardcoded que o
   entrypoint dos nodes de kind embute.

Nenhum dos dois é contornável sem um patch de manifesto do kindnet ou um
symlink "fake" do socket em `/var/run` (que exigiria root e mascarar um
caminho do sistema) -- workarounds frágeis que o plano pediu pra NÃO
aplicar sem reportar antes. Por isso este engine fica só como stub: a
interface está pronta (`is_available()` documenta o motivo exato, `up()`/
`down()` levantam `EngineError` explicando o que falta), pronto pra
implementar de verdade se o host mudar (ex.: kernel com `/lib/modules`
clássico, ou kind ganhar suporte a socket path customizável)."""

from __future__ import annotations

import shutil

from .base import ClusterEngine, ClusterHandle, EngineAvailability, EngineError


class KindEngine(ClusterEngine):
    name = "kind"

    def is_available(self) -> EngineAvailability:
        binary = shutil.which("kind")
        if not binary:
            return EngineAvailability(
                ok=False,
                reason=(
                    "binário 'kind' não está no PATH deste host -- além disso, "
                    "mesmo instalado, este host (NixOS) não tem '/lib/modules' "
                    "clássico (módulos ficam em "
                    "/run/current-system/kernel-modules/lib/modules/<versão>) "
                    "e o socket do Podman é rootless em $XDG_RUNTIME_DIR, "
                    "nenhum dos dois no path hardcoded que os node-containers "
                    "de kind esperam -- ver docstring deste módulo para a "
                    "evidência completa"
                ),
            )
        return EngineAvailability(
            ok=False,
            reason=(
                "'kind' está instalado, mas as duas paredes estruturais "
                "deste host (mount de /lib/modules e path do socket do "
                "container runtime) seguem valendo -- não tentamos 'up()' "
                "sem confirmar isso resolvido; ver docstring deste módulo"
            ),
        )

    def up(self) -> ClusterHandle:
        raise EngineError(
            "engine 'kind' não suportado neste host -- rode is_available() "
            "e veja o motivo, ou escolha outro KREWHUB_SMOKE_K8S_ENGINE"
        )

    def down(self) -> None:
        return None
