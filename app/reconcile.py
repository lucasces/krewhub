"""Reconcile periódico das rotas do configurable-http-proxy (CHP).

Porquê existir: o CHP guarda a tabela de rotas SÓ em memória. Quando o
pod do CHP reinicia ou é recriado, ele volta com a tabela VAZIA
(`Proxying ... to (no default)` no log dele) e TODA rota se perde -- a
do próprio `krewhub-central` e a de cada pod-por-dev já provisionado. O
site inteiro passa a devolver 404 ("No service is registered at this
URL") em todo path, mesmo com o `krewhub-central` 1/1 Ready (as probes
de liveness/readiness batem no pod direto, não através do CHP, então não
enxergam esse buraco). Antes deste loop, o único registro de rota
acontecia uma única vez -- a self-route no startup do `krewhub-central`,
a de cada dev no `/provision` -- sem nenhuma re-sincronização: a
recuperação exigia um `kubectl rollout restart` manual do
`krewhub-central`.

Este módulo re-registra, de forma idempotente, as rotas que DEVERIAM
existir (a self-route a partir de `settings.self_host`; a de cada dev a
partir do estado persistido em SQLite) e que estão FALTANDO na tabela
atual do CHP. Roda num loop de fundo (ver o lifespan em app/main.py) a
cada `settings.chp_reconcile_interval_seconds`. Idempotente: um ciclo
sem deriva não re-registra nada e não loga ruído; só loga quando
restaura algo (dizendo o quê). Falhas são NÃO-fatais -- mesma régua do
auto-registro de startup: um ciclo que falha é registrado no sinal de
status (`last_status`/`consecutive_failures`), não derruba o processo,
e o próximo ciclo tenta de novo.

O sinal de status (`ReconcileStatus`) é exposto no `/healthz` pra que um
reconcile persistentemente quebrado (ex.: RBAC errado, CHP inalcançável)
fique visível sem precisar ler log."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field

from app import chp_client, k8s_manager, store
from app.config import Settings

logger = logging.getLogger("krewhub.reconcile")

# Statuses em `devs` que significam "este dev tem um pod no ar cuja rota
# o CHP deveria estar servindo". `routed` é o estado terminal de sucesso
# do /provision (pod Ready + rota registrada). `ready`/`reconciled` são
# estados intermediários do MESMO fluxo, antes do registro da rota --
# incluídos porque um CHP que reiniciou no meio de um provision também
# precisa dessas rotas replayadas assim que o pod estiver de pé.
# `closed`/`lobby_pending`/`route_registration_failed`/
# `timeout_waiting_ready` ficam DE FORA de propósito: ou o workload foi
# derrubado (`closed` -- replayar a rota mandaria tráfego pra um pod que
# não existe) ou nunca chegou a ter rota pra começo de conversa.
ACTIVE_ROUTE_STATUSES = frozenset({"routed", "ready", "reconciled"})


def _normalize_host(route_key: str) -> str:
    """Chaves de `/api/routes` do CHP em modo `--host-routing` vêm com
    uma barra inicial (`/krewhub.example.com`); o registro é por host sem
    barra. Normaliza pra comparar os dois lados pela mesma chave."""
    return route_key.lstrip("/")


@dataclass
class ReconcileStatus:
    """Sinal observável do loop -- exposto no /healthz. `last_status` é
    'ok' quando o último ciclo rodou inteiro sem exceção (tendo ou não
    restaurado rotas), 'error' quando falhou. `consecutive_failures`
    acumula ciclos que falharam em sequência -- é o que denuncia um
    reconcile persistentemente quebrado (vs. uma falha pontual)."""

    last_run_at: float | None = None
    last_status: str = "never_run"
    last_error: str | None = None
    last_restored: list[str] = field(default_factory=list)
    consecutive_failures: int = 0
    total_restored: int = 0

    def to_health(self) -> dict:
        return asdict(self)


def desired_routes(settings: Settings) -> dict[str, str]:
    """Mapa host -> target que o CHP DEVERIA estar servindo, montado a
    partir do estado persistido (NÃO do CHP): a self-route (se
    `self_host` configurado) e a rota de cada dev com pod ativo no
    SQLite. É o lado "fonte da verdade" da comparação do reconcile."""
    desired: dict[str, str] = {}
    if settings.self_host:
        desired[settings.self_host] = chp_client.self_target(settings)
    with store.connect(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT slug, namespace, host, status FROM devs"
        ).fetchall()
    for row in rows:
        if row["status"] not in ACTIVE_ROUTE_STATUSES:
            continue
        if not row["host"] or not row["slug"]:
            continue
        desired[row["host"]] = chp_client.dev_target(row["slug"], row["namespace"])
    return desired


def reconcile_once(settings: Settings, status: ReconcileStatus) -> list[str]:
    """Um ciclo: lista as rotas atuais do CHP, re-registra as que
    DEVERIAM existir e estão faltando, e devolve a lista de hosts
    restaurados (vazia = sem deriva). Idempotente -- uma rota já
    presente não é re-registrada. Atualiza `status` in-place.

    Levanta a exceção subjacente (CHPError, erro de k8s, sqlite) em caso
    de falha -- quem chama (o loop) é que decide que isso é não-fatal e
    registra no status. `reconcile_once` em si é usável direto (ex.:
    testes, ou um gatilho manual) e aí o chamador vê o erro de verdade."""
    desired = desired_routes(settings)
    if not desired:
        status.last_run_at = time.time()
        status.last_status = "ok"
        status.last_error = None
        status.last_restored = []
        status.consecutive_failures = 0
        return []

    c = k8s_manager.get_clients(settings)
    try:
        current = chp_client.list_routes(c, settings)
        present = {_normalize_host(k) for k in current}

        restored: list[str] = []
        for host, target in desired.items():
            if host in present:
                continue
            chp_client.register_route(c, settings, host=host, target=target)
            restored.append(host)
    except Exception as exc:
        status.last_run_at = time.time()
        status.last_status = "error"
        status.last_error = f"{type(exc).__name__}: {exc}"
        status.consecutive_failures += 1
        raise

    status.last_run_at = time.time()
    status.last_status = "ok"
    status.last_error = None
    status.last_restored = restored
    status.consecutive_failures = 0
    status.total_restored += len(restored)
    if restored:
        logger.warning(
            "reconcile do CHP restaurou %d rota(s) ausente(s): %s",
            len(restored),
            ", ".join(restored),
        )
    return restored


class ReconcileLoop:
    """Loop de fundo que chama `reconcile_once` a cada
    `settings.chp_reconcile_interval_seconds`, numa thread daemon.
    Thread (não asyncio task) de propósito: `reconcile_once` é
    inteiramente síncrono/bloqueante (exec no k8s via a lib `kubernetes`,
    SQLite) -- rodar num executor manteria o event loop livre, uma
    thread daemon dedicada é mais simples e não concorre com o tráfego
    HTTP. Falha de um ciclo NUNCA derruba o loop: é logada e registrada
    no status, o loop dorme e tenta de novo.

    `start`/`stop` são idempotentes. `stop` sinaliza o evento e o loop
    acorda do sleep via `Event.wait` (não um `time.sleep` cego), então
    o shutdown é imediato, sem esperar o intervalo inteiro."""

    def __init__(self, settings: Settings, status: ReconcileStatus | None = None) -> None:
        self.settings = settings
        self.status = status if status is not None else ReconcileStatus()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        interval = self.settings.chp_reconcile_interval_seconds
        while not self._stop.is_set():
            try:
                reconcile_once(self.settings, self.status)
            except Exception:
                logger.warning(
                    "ciclo do reconcile do CHP falhou (não-fatal, segue tentando)",
                    exc_info=True,
                )
            # Event.wait em vez de time.sleep -- acorda na hora se stop()
            # for chamado durante o intervalo (shutdown limpo).
            self._stop.wait(interval)

    def start(self) -> None:
        if self.settings.chp_reconcile_interval_seconds <= 0:
            logger.info(
                "reconcile do CHP desligado (KREWHUB_CHP_RECONCILE_INTERVAL_SECONDS<=0)"
            )
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="chp-reconcile", daemon=True
        )
        self._thread.start()
        logger.info(
            "reconcile do CHP iniciado -- a cada %ss",
            self.settings.chp_reconcile_interval_seconds,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
