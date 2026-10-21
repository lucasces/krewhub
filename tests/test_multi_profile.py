"""Multi-profile: um mesmo owner_id com N sessões de dev provisionadas em
paralelo (ex.: "default" e "work"), cada uma totalmente isolada
(namespace-resources via slug próprio, host, extensões e geração de
sessão/revogação por profile). Backward-compat: o profile `default`
reproduz bit-a-bit o comportamento single-session de antes, e qualquer
linha de um banco pré-migração é mapeada pra `default`.

k8s/CHP/kirocrew 100% mockados (ver conftest.py) -- estes testes
exercitam store.py/auth.py/k8s_templates.py/main.py sem cluster real."""

from __future__ import annotations

import sqlite3
import urllib.parse
from unittest import mock

import pytest
from starlette.testclient import TestClient

from app import auth, chp_client, k8s_manager, kiro_login, session_client, store
from app.k8s_templates import host_for, slugify

OWNER = "dev-a@test.local"


def _path(owner_id: str) -> str:
    return urllib.parse.quote(owner_id, safe="")


# ---------------------------------------------------------------------------
# store.py -- dimensão profile, isolamento e migração
# ---------------------------------------------------------------------------


def test_default_profile_is_the_backward_compatible_default(settings):
    """Toda accessor de store usa profile='default' quando não informado
    -- é o que mantém o código single-session de antes funcionando sem
    mudança."""
    with store.connect(settings.db_path) as conn:
        store.upsert(
            conn, owner_id=OWNER, slug="s", namespace="krewhub-devs", host="h", status="routed"
        )
        # Lido sem passar profile == lido com profile='default'.
        assert store.get(conn, OWNER) is not None
        assert store.get(conn, OWNER, "default") is not None
        # Nenhum outro profile existe só porque o default existe.
        assert store.get(conn, OWNER, "work") is None


def test_two_profiles_of_the_same_owner_are_independent_rows(settings):
    with store.connect(settings.db_path) as conn:
        store.upsert(conn, owner_id=OWNER, profile="default", slug="dev-a-test-local",
                     namespace="krewhub-devs", host="dev-a-test-local.kiro.internal", status="routed")
        store.upsert(conn, owner_id=OWNER, profile="work", slug="dev-a-test-local-work",
                     namespace="krewhub-devs", host="dev-a-test-local-work.kiro.internal", status="routed")

        default_row = store.get(conn, OWNER, "default")
        work_row = store.get(conn, OWNER, "work")
        assert default_row["slug"] == "dev-a-test-local"
        assert work_row["slug"] == "dev-a-test-local-work"
        assert default_row["host"] != work_row["host"]

        # Fechar o "work" não toca no "default".
        store.upsert(conn, owner_id=OWNER, profile="work", slug=work_row["slug"],
                     namespace=work_row["namespace"], host=work_row["host"], status="closed")
        assert store.get(conn, OWNER, "default")["status"] == "routed"
        assert store.get(conn, OWNER, "work")["status"] == "closed"


def test_list_profiles_returns_every_profile_of_the_owner(settings):
    with store.connect(settings.db_path) as conn:
        store.upsert(conn, owner_id=OWNER, profile="default", slug="a", namespace="n", host="h1", status="routed")
        store.upsert(conn, owner_id=OWNER, profile="work", slug="a-work", namespace="n", host="h2", status="ready")
        store.upsert(conn, owner_id="other@test.local", profile="default", slug="o", namespace="n", host="h3", status="routed")
        rows = store.list_profiles(conn, OWNER)
    assert [r["profile"] for r in rows] == ["default", "work"]  # ordenado por profile
    assert all(r["owner_id"] == OWNER for r in rows)


def test_session_generation_is_scoped_per_profile(settings):
    with store.connect(settings.db_path) as conn:
        assert store.bump_session_generation(conn, OWNER, "work") == 1
        assert store.bump_session_generation(conn, OWNER, "work") == 2
        # "default" nunca foi bumpado -- segue em 0.
        assert store.get_session_generation(conn, OWNER, "default") == 0
        assert store.get_session_generation(conn, OWNER, "work") == 2


def test_extensions_state_is_scoped_per_profile(settings):
    with store.connect(settings.db_path) as conn:
        store.upsert_extension(conn, OWNER, "demo", enabled=True, profile="default")
        store.upsert_extension(conn, OWNER, "demo", enabled=False, profile="work")
        assert store.get_extension(conn, OWNER, "demo", "default")["enabled"] is True
        assert store.get_extension(conn, OWNER, "demo", "work")["enabled"] is False
        assert list(store.list_extensions(conn, OWNER, "default").keys()) == ["demo"]


def test_pre_migration_rows_are_mapped_to_the_default_profile(tmp_path):
    """Backward-compat da migração: um banco criado ANTES desta fatia
    (PK só owner_id / (owner_id, ext_id), sem coluna `profile`) é
    reconstruído e cada linha vira profile='default' -- o comportamento
    single-session de antes continua intacto depois do upgrade."""
    db = str(tmp_path / "legacy.db")
    # Monta o schema ANTIGO à mão (sem `profile`), como estava em main.
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE devs (owner_id TEXT PRIMARY KEY, slug TEXT NOT NULL, "
        "namespace TEXT NOT NULL, host TEXT NOT NULL, status TEXT NOT NULL, "
        "detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    old.execute(
        "CREATE TABLE session_generations (owner_id TEXT PRIMARY KEY, generation INTEGER NOT NULL)"
    )
    old.execute(
        "CREATE TABLE dev_extensions (owner_id TEXT NOT NULL, ext_id TEXT NOT NULL, "
        "enabled INTEGER NOT NULL DEFAULT 0, config_json TEXT NOT NULL DEFAULT '{}', "
        "runtime_state_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL, "
        "PRIMARY KEY (owner_id, ext_id))"
    )
    old.execute(
        "INSERT INTO devs (owner_id, slug, namespace, host, status, detail, created_at, updated_at) "
        "VALUES ('legacy@test.local', 'legacy-slug', 'krewhub-devs', 'legacy.kiro.internal', 'routed', '', 'x', 'x')"
    )
    old.execute("INSERT INTO session_generations (owner_id, generation) VALUES ('legacy@test.local', 3)")
    old.execute(
        "INSERT INTO dev_extensions (owner_id, ext_id, enabled, updated_at) "
        "VALUES ('legacy@test.local', 'demo', 1, 'x')"
    )
    old.commit()
    old.close()

    # Primeira conexão pela store nova roda a migração de profile.
    with store.connect(db) as conn:
        row = store.get(conn, "legacy@test.local", "default")
        assert row is not None
        assert row["profile"] == "default"
        assert row["slug"] == "legacy-slug"
        assert row["status"] == "routed"
        assert store.get_session_generation(conn, "legacy@test.local", "default") == 3
        assert store.get_extension(conn, "legacy@test.local", "demo", "default")["enabled"] is True

    # Idempotente: reconectar não re-migra nem perde dados.
    with store.connect(db) as conn:
        assert store.get(conn, "legacy@test.local", "default")["slug"] == "legacy-slug"


# ---------------------------------------------------------------------------
# k8s_templates.slugify -- default idêntico ao slug legado, profile sufixado
# ---------------------------------------------------------------------------


def test_slugify_default_profile_is_identical_to_the_legacy_slug():
    """Garantia crítica de backward-compat: o slug do profile `default`
    é EXATAMENTE o de antes do parâmetro profile existir -- os
    Pods/PVCs/Secrets já no cluster casam pelos mesmos nomes."""
    assert slugify(OWNER) == slugify(OWNER, "default") == "dev-a-test-local"


def test_slugify_non_default_profile_gets_a_distinct_suffixed_slug():
    assert slugify(OWNER, "work") == "dev-a-test-local-work"
    assert slugify(OWNER, "work") != slugify(OWNER, "default")


def test_slugify_keeps_the_combined_slug_within_the_dns_label_budget():
    long_owner = "a-very-long-owner-identifier-that-would-fill-the-budget@example.com"
    combined = slugify(long_owner, "work")
    # Nome de Service `kirocrew-<slug>` precisa caber em 63 chars (RFC 1035).
    assert len(f"kirocrew-{combined}") <= 63
    assert combined.endswith("-work")


# ---------------------------------------------------------------------------
# auth.py -- profile no payload do token, com default legado
# ---------------------------------------------------------------------------


def test_token_round_trips_the_profile():
    token = auth.sign_session(OWNER, secret="s", ttl_seconds=60, gen=1, profile="work")
    payload = auth.verify_session_payload(token, secret="s")
    assert payload["profile"] == "work"
    assert payload["gen"] == 1


def test_token_without_profile_defaults_to_default():
    """Token legado (assinado antes do multi-profile, sem o campo
    'profile') é aceito e lido como profile='default'."""
    token = auth.sign_session(OWNER, secret="s", ttl_seconds=60)  # sem profile
    assert auth.verify_session_payload(token, secret="s")["profile"] == "default"


def test_token_with_empty_or_nonstring_profile_is_rejected():
    import json

    def _forge(profile_value) -> str:
        payload = json.dumps(
            {"owner_id": OWNER, "exp": 9999999999, "gen": 0, "profile": profile_value},
            separators=(",", ":"),
        ).encode()
        import hashlib
        import hmac

        payload_b64 = auth._b64e(payload)
        sig = hmac.new(b"s", payload_b64.encode(), hashlib.sha256).digest()
        return f"{payload_b64}.{auth._b64e(sig)}"

    for bad in ("", 123):
        with pytest.raises(auth.AuthTokenError):
            auth.verify_session_payload(_forge(bad), secret="s")


# ---------------------------------------------------------------------------
# main.py -- provisionar Home+Work em paralelo, revogação por profile
# ---------------------------------------------------------------------------


@pytest.fixture
def mocked_infra(monkeypatch, settings):
    """Mocka reconcile_dev/wait/register_route/issue_token + revoke/
    teardown -- registra as chamadas com o profile/slug reais pra os
    testes afirmarem o isolamento por profile."""
    calls = {"reconcile": [], "revoke": [], "teardown": [], "issue_token": []}

    def _reconcile(_settings, owner_id, plans=(), *, allow_recreate=True, profile="default"):
        slug = slugify(owner_id, profile)
        host = host_for(slug, _settings)
        calls["reconcile"].append({"owner_id": owner_id, "profile": profile, "slug": slug, "host": host})
        return {
            "owner_id": owner_id,
            "profile": profile,
            "slug": slug,
            "namespace": _settings.dev_namespace,
            "host": host,
            "steps": {"namespace": "exists", "pod": "created"},
        }

    def _revoke(_c, *, namespace, slug):
        calls["revoke"].append({"namespace": namespace, "slug": slug})
        return "ok"

    def _teardown(_c, *, namespace, slug):
        calls["teardown"].append({"namespace": namespace, "slug": slug})
        return {"namespace": namespace, "slug": slug, "steps": {}}

    def _issue(_c, *, namespace, slug, host, public_port, scheme="http", ttl="24h"):
        calls["issue_token"].append({"slug": slug, "host": host})
        return f"{scheme}://{host}:{public_port}/?token=tok-{slug}"

    monkeypatch.setattr(k8s_manager, "reconcile_dev", _reconcile)
    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    monkeypatch.setattr(k8s_manager, "wait_for_ready", lambda *a, **kw: True)
    monkeypatch.setattr(k8s_manager, "teardown_dev_workload", _teardown)
    monkeypatch.setattr(chp_client, "register_route", lambda _c, _s, *, host, target: {"host": host, "target": target, "status": 201})
    monkeypatch.setattr(session_client, "revoke_session", _revoke)
    monkeypatch.setattr(session_client, "issue_token_url", _issue)
    monkeypatch.setattr(kiro_login, "start_device_flow", lambda *a, **kw: {"already_logged_in": True})
    return calls


def test_provision_home_and_work_in_parallel_yields_two_isolated_sessions(
    client, mocked_infra, sign_cookie, settings
):
    headers = {"Authorization": f"Bearer {sign_cookie(OWNER)}"}
    # "default" (Home) e "work" (Work) provisionados em paralelo.
    r_home = client.post(f"/devs/{_path(OWNER)}/provision", headers=headers)
    r_work = client.post(f"/devs/{_path(OWNER)}/provision?profile=work", headers=headers)
    assert r_home.status_code == 200 and r_work.status_code == 200
    assert r_home.json()["profile"] == "default"
    assert r_work.json()["profile"] == "work"
    assert r_home.json()["slug"] != r_work.json()["slug"]
    assert r_home.json()["host"] != r_work.json()["host"]

    # Os dois registros coexistem no SQLite, cada um na sua linha.
    with store.connect(settings.db_path) as conn:
        profiles = {r["profile"] for r in store.list_profiles(conn, OWNER)}
    assert profiles == {"default", "work"}

    # Reconcile foi chamado uma vez por profile, com slugs distintos.
    reconciled = {c["profile"]: c["slug"] for c in mocked_infra["reconcile"]}
    assert set(reconciled) == {"default", "work"}
    assert reconciled["default"] != reconciled["work"]


def test_profiles_endpoint_lists_both_sessions(client, mocked_infra, sign_cookie, settings):
    headers = {"Authorization": f"Bearer {sign_cookie(OWNER)}"}
    client.post(f"/devs/{_path(OWNER)}/provision", headers=headers)
    client.post(f"/devs/{_path(OWNER)}/provision?profile=work", headers=headers)

    r = client.get(f"/devs/{_path(OWNER)}/profiles", headers=headers)
    assert r.status_code == 200
    body = r.json()
    listed = {p["profile"]: p for p in body["profiles"]}
    assert set(listed) == {"default", "work"}
    assert listed["default"]["lobby_url"] == f"/devs/{urllib.parse.quote(OWNER, safe='@')}/lobby"
    assert "profile=work" in listed["work"]["lobby_url"]


def test_get_dev_is_scoped_by_profile(client, mocked_infra, sign_cookie, settings):
    headers = {"Authorization": f"Bearer {sign_cookie(OWNER)}"}
    client.post(f"/devs/{_path(OWNER)}/provision", headers=headers)  # só default

    assert client.get(f"/devs/{_path(OWNER)}", headers=headers).status_code == 200
    # "work" ainda não provisionado -> 404, mesmo com default existindo.
    assert client.get(f"/devs/{_path(OWNER)}?profile=work", headers=headers).status_code == 404


def test_logout_of_one_profile_does_not_revoke_the_other(
    client, mocked_infra, sign_cookie, settings
):
    """O coração do multi-profile: revogação é POR profile. Um /logout
    com o cookie do profile "work" invalida só os tokens de "work" -- o
    token de "default" (home) segue valendo, e seu pod não é derrubado."""
    # Provisiona os dois profiles.
    home_token = sign_cookie(OWNER, profile="default")
    work_token = sign_cookie(OWNER, profile="work")
    client.post(f"/devs/{_path(OWNER)}/provision", headers={"Authorization": f"Bearer {home_token}"})
    client.post(f"/devs/{_path(OWNER)}/provision?profile=work", headers={"Authorization": f"Bearer {work_token}"})
    mocked_infra["teardown"].clear()

    # /logout com o cookie de "work".
    r = client.get("/logout", headers={"Authorization": f"Bearer {work_token}"}, follow_redirects=False)
    assert r.status_code == 302

    with store.connect(settings.db_path) as conn:
        assert store.get_session_generation(conn, OWNER, "work") == 1
        assert store.get_session_generation(conn, OWNER, "default") == 0

    # Só o pod do "work" foi derrubado.
    assert len(mocked_infra["teardown"]) == 1
    assert mocked_infra["teardown"][0]["slug"] == slugify(OWNER, "work")

    # Token de "work" agora rejeitado; token de "home" segue válido.
    assert client.get(f"/devs/{_path(OWNER)}?profile=work",
                      headers={"Authorization": f"Bearer {work_token}"}).status_code == 401
    assert client.get(f"/devs/{_path(OWNER)}",
                      headers={"Authorization": f"Bearer {home_token}"}).status_code == 200


def test_close_scopes_to_the_tokens_own_profile(client, mocked_infra, sign_cookie, settings):
    """/close age sobre o profile que o cookie representa, não sobre o
    default implícito."""
    home_token = sign_cookie(OWNER, profile="default")
    work_token = sign_cookie(OWNER, profile="work")
    client.post(f"/devs/{_path(OWNER)}/provision", headers={"Authorization": f"Bearer {home_token}"})
    client.post(f"/devs/{_path(OWNER)}/provision?profile=work", headers={"Authorization": f"Bearer {work_token}"})
    mocked_infra["teardown"].clear()

    assert client.get("/close", headers={"Authorization": f"Bearer {work_token}"}).status_code == 200

    # Fechou só o "work"; "default" segue routed.
    assert len(mocked_infra["teardown"]) == 1
    assert mocked_infra["teardown"][0]["slug"] == slugify(OWNER, "work")
    with store.connect(settings.db_path) as conn:
        assert store.get(conn, OWNER, "work")["status"] == "closed"
        assert store.get(conn, OWNER, "default")["status"] == "routed"
    # /close NÃO bumpa geração (igual ao comportamento single-session).
    with store.connect(settings.db_path) as conn:
        assert store.get_session_generation(conn, OWNER, "work") == 0


def test_invalid_profile_name_is_rejected_with_400(client, mocked_infra, sign_cookie):
    headers = {"Authorization": f"Bearer {sign_cookie(OWNER)}"}
    r = client.post(f"/devs/{_path(OWNER)}/provision?profile=NOT valid!", headers=headers)
    assert r.status_code == 400
    assert "profile" in r.json()["detail"]


def test_new_session_is_scoped_per_profile(client, mocked_infra, sign_cookie, settings):
    headers = {"Authorization": f"Bearer {sign_cookie(OWNER)}"}
    client.post(f"/devs/{_path(OWNER)}/provision?profile=work", headers=headers)

    # Reemissão pro "work" funciona; pro "default" (não provisionado) 404.
    r_work = client.post(f"/devs/{_path(OWNER)}/session?profile=work", headers=headers)
    assert r_work.status_code == 200
    assert r_work.json()["profile"] == "work"
    assert f"tok-{slugify(OWNER, 'work')}" in r_work.json()["dashboard_url_with_token"]
    assert client.post(f"/devs/{_path(OWNER)}/session", headers=headers).status_code == 404
