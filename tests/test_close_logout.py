"""GET /close vs GET /logout -- ver README, seção "`/close` derruba o
workload". `session_client.revoke_session` e `k8s_manager.
teardown_dev_workload` mockados (nenhum kubectl exec/API k8s real)."""

from __future__ import annotations

from unittest import mock

import pytest

from app import k8s_manager, session_client, store


@pytest.fixture
def provisioned(settings):
    with store.connect(settings.db_path) as conn:
        store.upsert(
            conn,
            owner_id="dev-a@test.local",
            slug="dev-a-test-local",
            namespace="krewhub-devs",
            host="dev-a-test-local.kiro.internal",
            status="routed",
        )


@pytest.fixture
def mocked_revoke(monkeypatch):
    calls = []

    def _fake(_c, *, namespace, slug):
        calls.append({"namespace": namespace, "slug": slug})
        return "✅ All dashboard sessions revoked."

    monkeypatch.setattr(session_client, "revoke_session", _fake)
    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    return calls


@pytest.fixture
def mocked_teardown(monkeypatch):
    """`k8s_manager.teardown_dev_workload` mockado -- os testes deste
    arquivo cobrem SÓ a orquestração em app/main.py (ordem revoke ->
    teardown, melhor esforço, atualização do status no SQLite); o
    comportamento de baixo nível (ignore_not_found, PVC/Secret
    preservados) é coberto em tests/test_k8s_manager.py."""
    calls = []

    def _fake(_c, *, namespace, slug):
        calls.append({"namespace": namespace, "slug": slug})
        return {
            "namespace": namespace,
            "slug": slug,
            "steps": {
                "pod": "deleted",
                "service": "deleted",
                "networkpolicy": "deleted",
                "configmap": "deleted",
            },
        }

    monkeypatch.setattr(k8s_manager, "teardown_dev_workload", _fake)
    return calls


# ---------------------------------------------------------------------------
# GET /close
# ---------------------------------------------------------------------------


def test_close_requires_session(client, provisioned, mocked_revoke, mocked_teardown):
    r = client.get("/close")
    assert r.status_code == 401
    assert mocked_revoke == []
    assert mocked_teardown == []


def test_close_revokes_kirocrew_session_then_tears_down_workload_keeps_krewhub_cookie(
    client, provisioned, sign_cookie, mocked_revoke, mocked_teardown, settings
):
    """Cenário "close com sucesso": revoga a sessão do kirocrew E derruba
    Pod/Service/NetworkPolicy/ConfigMap (mockado) -- não toca no
    cookie do KrewHub, e o status no SQLite passa pra "closed" sem apagar
    a linha (login_mode etc. continuam lá, mesmo vazios aqui)."""
    cookie = sign_cookie("dev-a@test.local")
    client.cookies.set("krewhub_session", cookie)
    r = client.get("/close", follow_redirects=False)

    assert r.status_code == 200
    assert "desligado" in r.text.lower()
    assert "set-cookie" not in {h.lower() for h in r.headers.keys()}
    assert len(mocked_revoke) == 1
    assert mocked_revoke[0] == {"namespace": "krewhub-devs", "slug": "dev-a-test-local"}
    assert len(mocked_teardown) == 1
    assert mocked_teardown[0] == {"namespace": "krewhub-devs", "slug": "dev-a-test-local"}
    # O cookie do KrewHub continua o mesmo, servido pelo TestClient na
    # próxima chamada -- "continua logado" de verdade.
    assert client.cookies.get("krewhub_session") == cookie

    with store.connect(settings.db_path) as conn:
        row = store.get(conn, "dev-a@test.local")
    assert row["status"] == "closed"
    assert row["namespace"] == "krewhub-devs"
    assert row["slug"] == "dev-a-test-local"


def test_close_owner_id_comes_from_cookie_not_query(
    client, provisioned, sign_cookie, mocked_revoke, mocked_teardown
):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    client.get("/close?owner_id=dev-b@test.local")
    assert mocked_revoke[0]["slug"] == "dev-a-test-local"
    assert mocked_teardown[0]["slug"] == "dev-a-test-local"


def test_close_404_when_owner_never_provisioned(client, sign_cookie, mocked_revoke, mocked_teardown):
    client.cookies.set("krewhub_session", sign_cookie("dev-nunca-provisionado@test.local"))
    r = client.get("/close")
    assert r.status_code == 404
    assert mocked_teardown == []


def test_close_revocation_fails_teardown_still_succeeds(
    client, provisioned, sign_cookie, monkeypatch, mocked_teardown, settings
):
    """Cenário "close quando a revogação da sessão falha mas o teardown
    segue": revocação melhor-esforço falhando (ex.: pod já não responde)
    NÃO pode bloquear a exclusão do workload -- ainda 200, workload
    derrubado, status "closed" persistido."""
    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    monkeypatch.setattr(
        session_client,
        "revoke_session",
        mock.Mock(side_effect=session_client.SessionError("kirocrew logout não confirmou sucesso")),
    )
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/close")

    assert r.status_code == 200
    assert len(mocked_teardown) == 1
    with store.connect(settings.db_path) as conn:
        row = store.get(conn, "dev-a@test.local")
    assert row["status"] == "closed"


def test_close_502_when_teardown_itself_fails(
    client, provisioned, sign_cookie, mocked_revoke, monkeypatch
):
    """Diferente da revogação, uma falha REAL no teardown (não-404, ex.
    RBAC) NÃO é melhor esforço -- é a ação principal do endpoint agora,
    então vira 502."""
    monkeypatch.setattr(
        k8s_manager,
        "teardown_dev_workload",
        mock.Mock(side_effect=k8s_manager.TeardownError("falha ao deletar Pod 'kirocrew-dev-a-test-local'")),
    )
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/close")
    assert r.status_code == 502
    assert len(mocked_revoke) == 1  # revogação rodou antes do teardown falhar


# ---------------------------------------------------------------------------
# GET /logout
# ---------------------------------------------------------------------------


def test_logout_revokes_kirocrew_tears_down_workload_clears_krewhub_cookie_and_redirects(
    client, provisioned, sign_cookie, mocked_revoke, mocked_teardown, settings
):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/logout", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    set_cookie = r.headers["set-cookie"]
    assert "krewhub_session=" in set_cookie
    assert "Max-Age=0" in set_cookie
    assert len(mocked_revoke) == 1
    assert mocked_revoke[0] == {"namespace": "krewhub-devs", "slug": "dev-a-test-local"}
    assert len(mocked_teardown) == 1
    assert mocked_teardown[0] == {"namespace": "krewhub-devs", "slug": "dev-a-test-local"}
    with store.connect(settings.db_path) as conn:
        row = store.get(conn, "dev-a@test.local")
    assert row["status"] == "closed"


def test_logout_without_cookie_is_idempotent_no_revoke_attempted(client, mocked_revoke, mocked_teardown):
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert "Max-Age=0" in r.headers["set-cookie"]
    assert mocked_revoke == []
    assert mocked_teardown == []


def test_logout_with_malformed_cookie_never_errors(client, mocked_revoke, mocked_teardown):
    client.cookies.set("krewhub_session", "garbage-not-a-token")
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert mocked_revoke == []
    assert mocked_teardown == []


def test_logout_best_effort_when_revocation_raises_still_tears_down_and_clears_cookie(
    client, provisioned, sign_cookie, monkeypatch, mocked_teardown, settings
):
    """A revogação do kirocrew é melhor esforço dentro de /logout -- uma
    falha aqui NUNCA pode travar o logout do KrewHub em si nem impedir o
    teardown do workload (mesma regra de /close)."""
    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    monkeypatch.setattr(
        session_client,
        "revoke_session",
        mock.Mock(side_effect=session_client.SessionError("gateway indisponível")),
    )
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/logout", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert "Max-Age=0" in r.headers["set-cookie"]
    assert len(mocked_teardown) == 1
    with store.connect(settings.db_path) as conn:
        row = store.get(conn, "dev-a@test.local")
    assert row["status"] == "closed"


def test_logout_best_effort_when_teardown_itself_fails_still_clears_cookie_and_redirects(
    client, provisioned, sign_cookie, mocked_revoke, monkeypatch
):
    """Diferente de /close (onde falha real de teardown vira 502), em
    /logout até o teardown é melhor esforço -- a garantia de nunca vazar
    erro é mais forte que em /close."""
    monkeypatch.setattr(
        k8s_manager,
        "teardown_dev_workload",
        mock.Mock(side_effect=k8s_manager.TeardownError("falha ao deletar Service")),
    )
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/logout", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert "Max-Age=0" in r.headers["set-cookie"]


def test_logout_best_effort_when_owner_never_provisioned_still_logs_out(
    client, sign_cookie, mocked_revoke, mocked_teardown
):
    """owner_id válido no cookie, mas nunca provisionado (sem
    namespace/slug pra revogar/derrubar) -- ainda assim o /logout do
    KrewHub completa normalmente."""
    client.cookies.set("krewhub_session", sign_cookie("dev-nunca-provisionado@test.local"))
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert "Max-Age=0" in r.headers["set-cookie"]
    assert mocked_revoke == []
    assert mocked_teardown == []


# ---------------------------------------------------------------------------
# /provision depois de um /close anterior -- idempotente, mesmo slug/PVC
# ---------------------------------------------------------------------------


def test_provision_after_close_reconciles_again_with_same_slug_and_namespace(
    client, provisioned, sign_cookie, mocked_revoke, mocked_teardown, monkeypatch, settings
):
    """Cenário "provision idempotente depois de um close anterior":
    depois do /close marcar status="closed" (workload derrubado, PVC/
    Secret preservados por construção -- teardown mockado aqui, o
    comportamento real é coberto em test_k8s_manager.py), chamar
    POST /provision de novo tem que reconciliar com o MESMO slug/
    namespace (mesmo PVC, portanto mesmo workspace) e deixar o status
    de volta pra "routed" -- nunca cria um owner/slug novo."""
    from app import chp_client, k8s_manager as k8sm, session_client as sc
    from app.k8s_templates import host_for, slugify

    def _fake_reconcile(_settings, owner_id):
        slug = slugify(owner_id)
        host = host_for(slug, _settings)
        return {
            "owner_id": owner_id,
            "slug": slug,
            "namespace": _settings.dev_namespace,
            "host": host,
            "steps": {
                "namespace": "exists",
                "secret": "updated",
                "configmap": "created",
                "pvc": "updated",
                "service": "created",
                "networkpolicy": "created",
                "pod": "created",
            },
        }

    monkeypatch.setattr(k8sm, "reconcile_dev", _fake_reconcile)
    monkeypatch.setattr(k8sm, "wait_for_ready", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        chp_client, "register_route", lambda _c, _s, *, host, target: {"host": host, "target": target, "status": 201}
    )
    monkeypatch.setattr(
        sc,
        "issue_token_url",
        lambda _c, *, namespace, slug, host, public_port, scheme="http", ttl="24h": (
            f"{scheme}://{host}:{public_port}/?token=fake-after-close-{slug}"
        ),
    )

    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))

    r_close = client.get("/close")
    assert r_close.status_code == 200
    with store.connect(settings.db_path) as conn:
        closed_row = store.get(conn, "dev-a@test.local")
    assert closed_row["status"] == "closed"

    r_provision = client.post("/devs/dev-a%40test.local/provision")
    assert r_provision.status_code == 200
    body = r_provision.json()
    assert body["ready"] is True
    # mesmo slug/namespace de antes do /close -- prova de que reaproveita
    # o MESMO PVC (o teardown nunca tocou nele), não cria um dev novo.
    assert body["slug"] == closed_row["slug"] == "dev-a-test-local"
    assert body["namespace"] == closed_row["namespace"] == "krewhub-devs"

    with store.connect(settings.db_path) as conn:
        reprovisioned_row = store.get(conn, "dev-a@test.local")
    assert reprovisioned_row["status"] == "routed"
    assert reprovisioned_row["slug"] == "dev-a-test-local"
