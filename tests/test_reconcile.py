"""app/reconcile.py -- re-sincronização periódica das rotas do CHP
(issue #7: o CHP perde a tabela de rotas em memória ao reiniciar; o
krewhub-central nunca re-registrava, o site caía em 404 até um restart
manual).

k8s/CHP 100% mockados: `k8s_manager.get_clients` vira um Mock e
`chp_client.list_routes`/`register_route` são substituídos por fakes que
simulam a tabela do CHP em memória -- incluindo o cenário central do
issue, a tabela VAZIA logo após um restart do pod. SQLite real, arquivo
tmp por teste (`settings` fixture já isola)."""

from __future__ import annotations

from unittest import mock

import pytest

from app import chp_client, k8s_manager, reconcile, store
from app.config import Settings


@pytest.fixture
def recon_settings(settings) -> Settings:
    """Reaproveita a `settings` isolada do conftest (SQLite tmp), mas com
    `self_host` setado -- a self-route só entra em `desired_routes`
    quando há um host configurado."""
    from dataclasses import replace

    return replace(settings, self_host="krewhub.kiro.internal", self_port=8080)


@pytest.fixture
def fake_chp(monkeypatch):
    """Simula a tabela de rotas do CHP em memória. `table` é o estado
    atual (chaves com a barra inicial do host-routing, como o CHP real
    devolve); `register` escreve nela, `listed` conta as listagens.
    `k8s_manager.get_clients` vira um Mock (nenhum cluster tocado)."""
    state = {"table": {}, "registered": [], "listed": 0}

    def _list_routes(_c, _settings):
        state["listed"] += 1
        return dict(state["table"])

    def _register_route(_c, _settings, *, host, target):
        state["table"][f"/{host}"] = {"target": target}
        state["registered"].append((host, target))
        return {"host": host, "target": target, "status": 201}

    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    monkeypatch.setattr(chp_client, "list_routes", _list_routes)
    monkeypatch.setattr(chp_client, "register_route", _register_route)
    return state


def _provision_row(settings, owner_id, slug, status="routed"):
    with store.connect(settings.db_path) as conn:
        store.upsert(
            conn,
            owner_id=owner_id,
            slug=slug,
            namespace=settings.dev_namespace,
            host=f"{slug}.kiro.internal",
            status=status,
        )


# --- desired_routes (lado "fonte da verdade") -------------------------


def test_desired_routes_includes_self_and_active_devs(recon_settings):
    _provision_row(recon_settings, "dev-a@test.local", "dev-a-test-local", status="routed")
    _provision_row(recon_settings, "dev-b@test.local", "dev-b-test-local", status="ready")

    desired = reconcile.desired_routes(recon_settings)

    assert desired["krewhub.kiro.internal"] == chp_client.self_target(recon_settings)
    assert desired["dev-a-test-local.kiro.internal"] == chp_client.dev_target(
        "dev-a-test-local", recon_settings.dev_namespace
    )
    assert "dev-b-test-local.kiro.internal" in desired


def test_desired_routes_excludes_closed_and_pending_devs(recon_settings):
    _provision_row(recon_settings, "active@test.local", "active-test-local", status="routed")
    _provision_row(recon_settings, "closed@test.local", "closed-test-local", status="closed")
    _provision_row(recon_settings, "pending@test.local", "pending-test-local", status="lobby_pending")

    desired = reconcile.desired_routes(recon_settings)

    assert "active-test-local.kiro.internal" in desired
    assert "closed-test-local.kiro.internal" not in desired
    assert "pending-test-local.kiro.internal" not in desired


def test_desired_routes_omits_self_when_self_host_unset(settings):
    # `settings` (conftest) tem self_host="" -- sem rota própria.
    assert reconcile.desired_routes(settings) == {}


# --- reconcile_once (o coração do fix) --------------------------------


def test_reconcile_restores_central_and_dev_routes_into_empty_table(recon_settings, fake_chp):
    """CENÁRIO DO ISSUE: o pod do CHP reiniciou e voltou com a tabela
    VAZIA. O reconcile re-registra a self-route E as rotas dos devs já
    provisionados -- sem precisar reiniciar o krewhub-central."""
    _provision_row(recon_settings, "dev-a@test.local", "dev-a-test-local", status="routed")
    _provision_row(recon_settings, "dev-b@test.local", "dev-b-test-local", status="routed")
    assert fake_chp["table"] == {}  # tabela vazia pós-restart

    status = reconcile.ReconcileStatus()
    restored = reconcile.reconcile_once(recon_settings, status)

    assert set(restored) == {
        "krewhub.kiro.internal",
        "dev-a-test-local.kiro.internal",
        "dev-b-test-local.kiro.internal",
    }
    # Tabela do CHP agora tem as 3 rotas de volta.
    assert set(k.lstrip("/") for k in fake_chp["table"]) == set(restored)
    assert status.last_status == "ok"
    assert status.consecutive_failures == 0
    assert status.total_restored == 3


def test_reconcile_is_idempotent_when_routes_already_present(recon_settings, fake_chp):
    """Rotas já presentes não são re-registradas -- um segundo ciclo sem
    deriva não toca no CHP e não acusa restauração."""
    _provision_row(recon_settings, "dev-a@test.local", "dev-a-test-local", status="routed")

    status = reconcile.ReconcileStatus()
    first = reconcile.reconcile_once(recon_settings, status)
    assert len(first) == 2  # self + dev-a
    registered_after_first = len(fake_chp["registered"])

    second = reconcile.reconcile_once(recon_settings, status)
    assert second == []
    assert len(fake_chp["registered"]) == registered_after_first  # nada re-registrado
    assert status.last_restored == []
    assert status.last_status == "ok"


def test_reconcile_only_restores_the_missing_route(recon_settings, fake_chp):
    """Deriva parcial: a self-route sobreviveu mas a de um dev sumiu --
    só a que falta é restaurada."""
    _provision_row(recon_settings, "dev-a@test.local", "dev-a-test-local", status="routed")
    # CHP ainda tem só a self-route (barra inicial, como o real).
    fake_chp["table"]["/krewhub.kiro.internal"] = {
        "target": chp_client.self_target(recon_settings)
    }

    status = reconcile.ReconcileStatus()
    restored = reconcile.reconcile_once(recon_settings, status)

    assert restored == ["dev-a-test-local.kiro.internal"]


def test_reconcile_does_not_replay_closed_dev_routes(recon_settings, fake_chp):
    """Um dev cujo workload foi derrubado (`closed`) NÃO tem a rota
    replayada -- mandaria tráfego pra um pod que não existe mais."""
    _provision_row(recon_settings, "closed@test.local", "closed-test-local", status="closed")

    status = reconcile.ReconcileStatus()
    restored = reconcile.reconcile_once(recon_settings, status)

    # Só a self-route é restaurada; a do dev fechado, não.
    assert restored == ["krewhub.kiro.internal"]


def test_reconcile_records_failure_as_non_fatal_signal(recon_settings, fake_chp, monkeypatch):
    """Uma falha ao listar as rotas (CHP inalcançável, RBAC errado)
    propaga de `reconcile_once`, mas atualiza o sinal de status
    (`last_status=error`, `consecutive_failures++`) -- é isso que o loop
    captura como não-fatal e o /healthz expõe."""
    _provision_row(recon_settings, "dev-a@test.local", "dev-a-test-local", status="routed")

    def _boom(_c, _s):
        raise chp_client.CHPError("CHP inalcançável")

    monkeypatch.setattr(chp_client, "list_routes", _boom)

    status = reconcile.ReconcileStatus()
    with pytest.raises(chp_client.CHPError):
        reconcile.reconcile_once(recon_settings, status)

    assert status.last_status == "error"
    assert status.consecutive_failures == 1
    assert "CHP inalcançável" in status.last_error


def test_reconcile_failure_counter_resets_after_a_good_cycle(recon_settings, fake_chp, monkeypatch):
    _provision_row(recon_settings, "dev-a@test.local", "dev-a-test-local", status="routed")
    status = reconcile.ReconcileStatus()

    def _boom(_c, _s):
        raise chp_client.CHPError("transitório")

    monkeypatch.setattr(chp_client, "list_routes", _boom)
    for _ in range(3):
        with pytest.raises(chp_client.CHPError):
            reconcile.reconcile_once(recon_settings, status)
    assert status.consecutive_failures == 3

    # Volta a funcionar.
    monkeypatch.setattr(chp_client, "list_routes", lambda _c, _s: {})
    reconcile.reconcile_once(recon_settings, status)
    assert status.consecutive_failures == 0
    assert status.last_status == "ok"


def test_reconcile_noop_when_nothing_desired(settings, fake_chp):
    """Sem self_host e sem devs ativos -- ciclo vazio, não bate no CHP,
    status ok."""
    status = reconcile.ReconcileStatus()
    assert reconcile.reconcile_once(settings, status) == []
    assert fake_chp["listed"] == 0  # nem chegou a listar
    assert status.last_status == "ok"


# --- ReconcileLoop (thread de fundo) ----------------------------------


def test_loop_disabled_when_interval_non_positive(recon_settings):
    from dataclasses import replace

    loop = reconcile.ReconcileLoop(replace(recon_settings, chp_reconcile_interval_seconds=0))
    loop.start()
    assert loop._thread is None  # não subiu thread
    loop.stop()


def test_loop_runs_at_least_one_cycle_and_stops_clean(recon_settings, fake_chp):
    import time
    from dataclasses import replace

    _provision_row(recon_settings, "dev-a@test.local", "dev-a-test-local", status="routed")
    loop = reconcile.ReconcileLoop(
        replace(recon_settings, chp_reconcile_interval_seconds=60)
    )
    loop.start()
    # O primeiro ciclo roda imediatamente (antes do primeiro wait).
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and loop.status.last_run_at is None:
        time.sleep(0.02)
    loop.stop()

    assert loop.status.last_run_at is not None
    assert loop.status.last_status == "ok"
    assert "/dev-a-test-local.kiro.internal" in fake_chp["table"]
    assert loop._thread is None


# --- /healthz expõe o sinal -------------------------------------------


def test_healthz_exposes_reconcile_status(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "reconcile" in body
    assert set(body["reconcile"]) >= {
        "last_status",
        "consecutive_failures",
        "total_restored",
    }
