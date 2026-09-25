"""Interface comum de "engine" de cluster Kubernetes efêmero pro
smoke-test.

Por que essa abstração existe: kind e k3d bateram em paredes estruturais
neste host (NixOS, sem `/lib/modules` clássico, cgroups/kernel não
expondo o que o driver docker-in-docker de kind/k3d espera -- ver
`SmokeReport` pra evidência exata de cada tentativa). Em vez de
hardcodar "o jeito que funcionou aqui" (podman machine) no script de
smoke-test, o script principal (`run_smoke.py`) só fala com esta
interface -- trocar de engine no futuro (voltar pra kind/k3d se o host
mudar, apontar pra um cluster gerenciado, etc.) não deve exigir tocar em
`run_smoke.py` nem no fake-kirocrew.

Contrato mínimo, deliberadamente pequeno (3 métodos):
  - `is_available()` -- checagem de pré-requisito, RÁPIDA e sem efeito
    colateral (não sobe nada). Retorna `EngineAvailability` com motivo
    explícito quando `False` -- nunca um `False` silencioso.
  - `up()` -- sobe (ou reaproveita, se já existir) o cluster efêmero e
    devolve um `ClusterHandle` com kubeconfig/contexto prontos pra uso
    imediato por `kubectl`/pelo client Python `kubernetes` padrão --
    nenhuma chamada fora dessa interface deveria precisar saber COMO o
    cluster foi criado.
  - `down()` -- destrói tudo que `up()` criou. Idempotente: chamar
    `down()` sem `up()` bem-sucedido antes não deve estourar.

Qualquer engine novo (voltar pra kind/k3d por outro caminho, um cluster
gerenciado externo, etc.) só precisa implementar esta classe."""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(frozen=True)
class EngineAvailability:
    ok: bool
    reason: str  # sempre preenchido, mesmo quando ok=True (o que foi checado)


@dataclass(frozen=True)
class ClusterHandle:
    """Tudo que o smoke-test precisa pra falar com o cluster efêmero,
    sem saber qual engine o criou.

    kubeconfig_path: path pra um kubeconfig usável IMEDIATAMENTE por
        `kubectl --kubeconfig ... --context ...` e pelo client Python
        `kubernetes.config.load_kube_config(...)` rodando no HOST (não
        dentro de alguma VM/container que o host não alcança) -- inclui
        qualquer rewrite de endereço/porta (ex.: túnel SSH local) que
        seja necessário pra isso ser verdade.
    context: nome do contexto dentro desse kubeconfig a usar.
    node_ip: IP (do ponto de vista do HOST) alcançável pra bater em
        NodePort/porta exposta do cluster -- usado pro passo final do
        smoke-test (curl real através do CHP, não só chamada da API k8s).
        Pode ser `None` se o engine preferir que o smoke-test use
        `kubectl port-forward` em vez de NodePort (também válido -- é o
        mesmo padrão já usado contra o cluster real de produção).
    """

    kubeconfig_path: str
    context: str
    node_ip: str | None = None


class ClusterEngine(abc.ABC):
    """Implementações concretas vivem em `smoke/engines/<nome>.py` e se
    registram em `smoke/engines/__init__.py::ENGINES`."""

    name: str

    @abc.abstractmethod
    def is_available(self) -> EngineAvailability:
        """Só checagem -- não deve criar nem modificar nada no host."""

    @abc.abstractmethod
    def up(self) -> ClusterHandle:
        """Sobe o cluster efêmero (ou reaproveita um já rodando com o
        mesmo nome/marcação desta engine) e devolve um handle pronto pra
        uso. Deve levantar `EngineError` com uma mensagem acionável se
        `is_available()` não teria passado -- não assume que o chamador
        sempre checou antes."""

    @abc.abstractmethod
    def down(self) -> None:
        """Destrói tudo que `up()` criou. Idempotente."""

    def load_image(self, build_dir: str, tag: str) -> str:
        """Método OPCIONAL (não faz parte do contrato mínimo dos 3
        acima) -- builda a imagem em `build_dir` (Dockerfile ali dentro)
        e a disponibiliza pro cluster efêmero SEM depender de um
        registry externo, devolvendo a referência de imagem pronta pra
        usar num manifest (`image: <o que isso devolver>`).

        Deliberadamente não-abstrato: COMO uma imagem local chega no
        cluster é o ponto mais específico de cada engine (`kind load
        docker-image`, `k3d image import`, build+ssh+`ctr images import`
        pra podman-machine) -- nem todo engine precisa disso (`external`
        pode preferir só builder+pushar pra um registry alcançável, ou
        nem suportar isso). Implementação default levanta
        `EngineError` explícito em vez de fingir suporte."""
        raise EngineError(
            f"engine '{self.name}' não implementa load_image() -- "
            "publique a imagem num registry alcançável pelo cluster e "
            "use a referência completa no manifest em vez disso"
        )


class EngineError(RuntimeError):
    pass
