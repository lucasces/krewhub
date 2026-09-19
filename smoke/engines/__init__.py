"""Registro dos engines de cluster efêmero disponíveis pro smoke-test.

Seleção via env var `KREWHUB_SMOKE_K8S_ENGINE` -- SEM default silencioso
(ver `run_smoke.py::select_engine`): se a env var não vier setada, o
script lista as opções conhecidas (com `is_available()` de cada uma) e
para, pedindo pra escolher explicitamente. Isso é deliberado -- rodar o
engine errado sem perceber (ex.: achar que testou contra kind mas caiu no
fallback external, que aponta pro cluster REAL) seria pior que exigir uma
escolha explícita."""

from __future__ import annotations

from .base import ClusterEngine, ClusterHandle, EngineAvailability, EngineError
from .external import ExternalEngine
from .k3d import K3dEngine
from .kind import KindEngine
from .podman_machine import PodmanMachineEngine

# Ordem = ordem de preferência quando alguém pedir "qual funciona aqui" --
# não é usada pra escolher um default silencioso (não existe default),
# só pra listar em ordem sensata.
ENGINES: dict[str, type[ClusterEngine]] = {
    "podman-machine": PodmanMachineEngine,
    "kind": KindEngine,
    "k3d": K3dEngine,
    "external": ExternalEngine,
}

__all__ = [
    "ClusterEngine",
    "ClusterHandle",
    "EngineAvailability",
    "EngineError",
    "ENGINES",
]
