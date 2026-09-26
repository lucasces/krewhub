"""GET /devs/{owner_id} (lookup de um dev especifico) -- sem tocar
k8s/CHP, so app/store.py. Ver app/main.py::get_dev."""

from __future__ import annotations

from app import store


def test_get_dev_requires_auth(client):
    r = client.get("/devs/dev-a%40test.local")
    assert r.status_code == 401


def test_get_dev_rejects_cross_owner(client, sign_cookie, settings):
    with store.connect(settings.db_path) as conn:
        store.upsert(
            conn,
            owner_id="dev-a@test.local",
            slug="dev-a-test-local",
            namespace="krewhub-devs",
            host="dev-a-test-local.kiro.internal",
            status="routed",
        )
    client.cookies.set("krewhub_session", sign_cookie("dev-b@test.local"))
    r = client.get("/devs/dev-a%40test.local")
    assert r.status_code == 403


def test_get_dev_succeeds_for_own_owner(client, sign_cookie, settings):
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
    r = client.get("/devs/dev-a%40test.local")
    assert r.status_code == 200
    body = r.json()
    assert body["owner_id"] == "dev-a@test.local"
    assert body["slug"] == "dev-a-test-local"


def test_get_dev_404_when_never_provisioned(client, sign_cookie):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.get("/devs/dev-a%40test.local")
    assert r.status_code == 404
