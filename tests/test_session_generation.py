"""session_generation -- revogacao de verdade pro proprio krewhub_session
(issue #2): /logout incrementa a geracao persistida em app/store.py;
qualquer token assinado ANTES do incremento passa a falhar mesmo com
assinatura/expiracao ainda validas. k8s/CHP/kirocrew mockados (ver
conftest.py) -- estes testes so exercitam auth.py/store.py/main.py."""

from __future__ import annotations

from unittest import mock

from app import auth, store


def test_get_session_generation_defaults_to_zero_when_never_set(settings):
    with store.connect(settings.db_path) as conn:
        assert store.get_session_generation(conn, "dev-never-seen@test.local") == 0


def test_bump_session_generation_increments_and_persists(settings):
    with store.connect(settings.db_path) as conn:
        assert store.bump_session_generation(conn, "dev-a@test.local") == 1
        assert store.bump_session_generation(conn, "dev-a@test.local") == 2
        assert store.get_session_generation(conn, "dev-a@test.local") == 2


def test_verify_session_payload_includes_gen():
    token = auth.sign_session("dev-a@test.local", secret="s", ttl_seconds=60, gen=3)
    payload = auth.verify_session_payload(token, secret="s")
    assert payload == {"owner_id": "dev-a@test.local", "exp": mock.ANY, "gen": 3}


def test_verify_session_still_returns_plain_owner_id_unaffected_by_gen():
    """Contrato antigo (usado por tests/test_auth_tokens.py) continua o
    mesmo -- so owner_id, sem exigir SQLite."""
    token = auth.sign_session("dev-a@test.local", secret="s", ttl_seconds=60, gen=7)
    assert auth.verify_session(token, secret="s") == "dev-a@test.local"


def test_token_signed_before_logout_is_rejected_after_logout_bumps_generation(
    client, sign_cookie, settings
):
    """Cenario central da issue #2: um token valido (assinatura + prazo
    OK) emitido ANTES de um /logout passa a ser rejeitado depois --
    reusa GET /devs/{owner_id} (tests/test_devs_lookup.py) como o
    endpoint protegido mais simples, sem precisar mockar k8s/CHP."""
    stale_token = sign_cookie("dev-a@test.local")  # gen=0, igual a geracao atual

    with store.connect(settings.db_path) as conn:
        store.bump_session_generation(conn, "dev-a@test.local")

    client.cookies.set("krewhub_session", stale_token)
    r = client.get("/devs/dev-a%40test.local")
    assert r.status_code == 401
    assert "revogada" in r.json()["detail"]


def test_freshly_signed_token_after_bump_still_works(client, sign_cookie, settings):
    with store.connect(settings.db_path) as conn:
        store.upsert(
            conn,
            owner_id="dev-a@test.local",
            slug="dev-a-test-local",
            namespace="krewhub-devs",
            host="dev-a-test-local.kiro.internal",
            status="routed",
        )
        new_gen = store.bump_session_generation(conn, "dev-a@test.local")

    fresh_token = sign_cookie("dev-a@test.local", gen=new_gen)
    client.cookies.set("krewhub_session", fresh_token)
    r = client.get("/devs/dev-a%40test.local")
    # 200 com o registro real -- nao 401/403 -- prova que a credencial
    # POS-bump (geracao correta) foi aceita normalmente.
    assert r.status_code == 200
    assert r.json()["slug"] == "dev-a-test-local"


def test_logout_bumps_generation_so_a_second_stale_tab_stops_working(
    client, sign_cookie, settings, monkeypatch
):
    """Fluxo real de ponta a ponta: duas 'abas' com o MESMO token
    valido; uma chama /logout, a outra deixa de conseguir usar o token
    antigo -- sem precisar de nenhum estado alem do que /logout ja
    grava no SQLite."""
    from app import k8s_manager, session_client

    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    monkeypatch.setattr(session_client, "revoke_session", lambda *_a, **_kw: "ok")
    monkeypatch.setattr(k8s_manager, "teardown_dev_workload", lambda *_a, **_kw: {"steps": {}})

    shared_token = sign_cookie("dev-a@test.local")

    tab1 = client
    tab1.cookies.set("krewhub_session", shared_token)
    r_logout = tab1.get("/logout", follow_redirects=False)
    assert r_logout.status_code == 302

    from starlette.testclient import TestClient

    import app.main as main

    with TestClient(main.app, base_url="http://krewhub.kiro.internal") as tab2:
        tab2.cookies.set("krewhub_session", shared_token)
        r_stale = tab2.get("/devs/dev-a%40test.local")
        assert r_stale.status_code == 401


def test_callback_signs_new_session_with_current_generation_not_stale_zero(
    settings, monkeypatch
):
    """Se um owner ja tinha feito /logout antes (geracao > 0), o
    PROXIMO /callback tem que assinar o novo token com a geracao atual
    -- nao com 0 -- senao o /logout anterior deixaria o dev
    permanentemente deslogado."""
    import app.main as main

    monkeypatch.setattr(main, "_settings", settings)
    monkeypatch.setattr(main, "_pending_logins", {"fake-state": "fake-verifier"})

    with store.connect(settings.db_path) as conn:
        store.bump_session_generation(conn, "dev-a@test.local")
        store.bump_session_generation(conn, "dev-a@test.local")

    def _fake_exchange(_settings, *, code, code_verifier):
        return {"owner_id": "dev-a@test.local", "claims": {}, "tokens": {}}

    monkeypatch.setattr(main, "exchange_code", _fake_exchange)

    from starlette.testclient import TestClient

    with TestClient(main.app, base_url="http://krewhub.kiro.internal") as c:
        r = c.get("/callback?code=abc&state=fake-state", follow_redirects=False)
        assert r.status_code == 302
        set_cookie = r.headers["set-cookie"]
        token = set_cookie.split("krewhub_session=")[1].split(";")[0]

    payload = auth.verify_session_payload(token, secret=settings.session_secret)
    assert payload["gen"] == 2
