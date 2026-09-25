"""Engine `external` -- não sobe nada; aponta pra um kubeconfig/contexto
já existente, configurado via env var. É o fallback manual: útil pra
apontar pra um namespace descartável dentro de um cluster real (inclusive
o próprio homelab) quando nenhum engine efêmero está
disponível, ou pra rodar o mesmo smoke-test contra um cluster de CI já
provisionado por outra ferramenta.

Diferente dos outros engines, este é "sempre disponível" na checagem de
pré-requisito (só existência do kubeconfig é checada) -- a
responsabilidade de não apontar pra produção por engano é de quem seta as
env vars, não deste código. `down()` é deliberadamente um no-op: este
engine NUNCA destrói o cluster apontado (não foi ele quem criou), só
existe pra permitir reconciliar/inspecionar; a limpeza dos recursos que o
PRÓPRIO smoke-test criou (namespace/objetos do owner de teste) é
responsabilidade do script principal (`run_smoke.py`), não do engine."""

from __future__ import annotations

import os

from .base import ClusterEngine, ClusterHandle, EngineAvailability, EngineError


class ExternalEngine(ClusterEngine):
    name = "external"

    def __init__(self) -> None:
        self._kubeconfig = os.environ.get(
            "KREWHUB_SMOKE_EXTERNAL_KUBECONFIG",
            os.path.expanduser("~/.kube/config-personal"),
        )
        self._context = os.environ.get("KREWHUB_SMOKE_EXTERNAL_CONTEXT", "")

    def is_available(self) -> EngineAvailability:
        if not os.path.isfile(self._kubeconfig):
            return EngineAvailability(
                ok=False,
                reason=f"kubeconfig '{self._kubeconfig}' não existe",
            )
        if not self._context:
            return EngineAvailability(
                ok=False,
                reason=(
                    "KREWHUB_SMOKE_EXTERNAL_CONTEXT não setado -- sem "
                    "default silencioso (não vamos assumir qual contexto "
                    "usar contra um kubeconfig que pode ter vários)"
                ),
            )
        return EngineAvailability(
            ok=True,
            reason=f"kubeconfig={self._kubeconfig} context={self._context}",
        )

    def up(self) -> ClusterHandle:
        availability = self.is_available()
        if not availability.ok:
            raise EngineError(f"engine 'external' indisponível: {availability.reason}")
        return ClusterHandle(kubeconfig_path=self._kubeconfig, context=self._context)

    def down(self) -> None:
        return None  # nunca destrói um cluster que este engine não criou
