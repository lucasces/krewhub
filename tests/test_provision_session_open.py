"""POST /devs/{owner_id}/provision, /session, GET /open -- k8s/CHP/
kirocrew exec 100% mockados (ver conftest.py). SQLite real, arquivo tmp
por teste."""

from __future__ import annotations

from unittest import mock

import pytest

from app import chp_client, k8s_manager, session_client, store


def _fake_reconcile_result(owner_id: str, slug: str, namespace: str, host: str) -> dict:
    return {
        "owner_id": owner_id,
        "slug": slug,
        "namespace": namespace,
        "host": host,
        "steps": {
            "namespace": "exists",
            "secret": "created",
            "configmap": "created",
            "pvc": "created",
            "service": "created",
            "networkpolicy": "created",
            "pod": "created",
        },
    }


@pytest.fixture
def mocked_infra(monkeypatch, settings):
    """Mocka tudo que tocaria k8s/CHP real -- reconcile_dev, get_clients,
    wait_for_ready, chp_client.register_route, session_client.issue_token_url."""
    from app.k8s_templates import host_for, slugify

    def _reconcile(_settings, owner_id):
        slug = slugify(owner_id)
        host = host_for(slug, _settings)
        return _fake_reconcile_result(owner_id, slug, _settings.dev_namespace, host)

    calls = {"reconcile": 0, "wait": 0, "register_route": 0, "issue_token": 0}

    def _reconcile_counted(_settings, owner_id):
        calls["reconcile"] += 1
        return _reconcile(_settings, owner_id)

    def _wait_for_ready(*_a, **_kw):
        calls["wait"] += 1
        return True

    def _register_route(_c, _settings, *, host, target):
        calls["register_route"] += 1
        return {"host": host, "target": target, "status": 201}

    def _issue_token_url(_c, *, namespace, slug, host, public_port, scheme="http", ttl="24h"):
        calls["issue_token"] += 1
        return f"{scheme}://{host}:{public_port}/?token=fake-token-for-{slug}"

    monkeypatch.setattr(k8s_manager, "reconcile_dev", _reconcile_counted)
    monkeypatch.setattr(k8s_manager, "get_clients", lambda _settings: mock.Mock())
    monkeypatch.setattr(k8s_manager, "wait_for_ready", _wait_for_ready)
    monkeypatch.setattr(chp_client, "register_route", _register_route)
    monkeypatch.setattr(session_client, "issue_token_url", _issue_token_url)
    return calls


def test_provision_requires_auth(client, mocked_infra):
    r = client.post("/devs/dev-a%40test.local/provision")
    assert r.status_code == 401


def test_provision_rejects_cross_owner_credential(client, mocked_infra, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie("dev-b@test.local"))
    r = client.post("/devs/dev-a%40test.local/provision")
    assert r.status_code == 403


def test_provision_succeeds_for_own_owner_and_returns_dashboard_url(client, mocked_infra, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/provision")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["route"]["status"] == 201
    assert body["dashboard_url_with_token"].startswith("http://dev-a-test-local.kiro.internal:8080/?token=")
    assert mocked_infra["reconcile"] == 1
    assert mocked_infra["wait"] == 1
    assert mocked_infra["register_route"] == 1


def test_provision_is_idempotent_across_repeated_calls(client, mocked_infra, sign_cookie, settings):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r1 = client.post("/devs/dev-a%40test.local/provision")
    r2 = client.post("/devs/dev-a%40test.local/provision")
    assert r1.status_code == r2.status_code == 200
    # Reconcile foi chamado de novo (não é "no-op silencioso") mas o
    # create-vs-patch de verdade é responsabilidade de k8s_manager
    # (testado separadamente, mockado aqui) -- o que garantimos NESTE
    # nível é que a 2a chamada não falha nem diverge de owner/slug.
    assert mocked_infra["reconcile"] == 2
    with store.connect(settings.db_path) as conn:
        row = store.get(conn, "dev-a@test.local")
    assert row["status"] == "routed"
    assert row["slug"] == "dev-a-test-local"


def test_session_404_when_never_provisioned(client, mocked_infra, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/session")
    assert r.status_code == 404


def test_session_reissues_token_when_already_provisioned(client, mocked_infra, sign_cookie, settings):
    with store.connect(settings.db_path) as conn:
        store.upsert(
            conn,
            owner_id="dev-a@test.local",
            slug="dev-a-test-local",
            namespace="krewhub-devs",
            host="dev-a-test-local.kiro.internal",
            status="routed",
        )
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/session")
    assert r.status_code == 200
    assert r.json()["dashboard_url_with_token"].startswith("http://dev-a-test-local.kiro.internal:8080/?token=")
    assert mocked_infra["issue_token"] == 1


def test_session_requires_auth(client, mocked_infra):
    r = client.post("/devs/dev-a%40test.local/session")
    assert r.status_code == 401


def test_open_redirects_with_token_embedded(client, mocked_infra, settings):
    with store.connect(settings.db_path) as conn:
        store.upsert(
            conn,
            owner_id="dev-a@test.local",
            slug="dev-a-test-local",
            namespace="krewhub-devs",
            host="dev-a-test-local.kiro.internal",
            status="routed",
        )
    r = client.get("/devs/dev-a%40test.local/open", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("http://dev-a-test-local.kiro.internal:8080/?token=")


def test_open_404_when_never_provisioned(client, mocked_infra):
    r = client.get("/devs/dev-a%40test.local/open", follow_redirects=False)
    assert r.status_code == 404
