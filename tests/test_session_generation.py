"""session_generation -- revogacao de verdade pro proprio krewhub_session
(issue #2): /logout incrementa a geracao persistida em app/store.py
(tabela `session_generations`, separada de `devs`); qualquer token
assinado ANTES do incremento passa a falhar mesmo com
assinatura/expiracao ainda validas. k8s/CHP/kirocrew mockados (ver
conftest.py) -- estes testes so exercitam auth.py/store.py/main.py."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from unittest import mock

import pytest
from starlette.testclient import TestClient

from app import auth, k8s_manager, session_client, store

OWNER_A = "dev-a@test.local"
OWNER_B = "dev-b@test.local"


def _path(owner_id: str) -> str:
    return urllib.parse.quote(owner_id, safe="")


def _provision(settings, owner_id: str) -> None:
    slug = owner_id.split("@")[0] + "-test-local"
    with store.connect(settings.db_path) as conn:
        store.upsert(
            conn,
            owner_id=owner_id,
            slug=slug,
            namespace="krewhub-devs",
            host=f"{slug}.kiro.internal",
            status="routed",
        )


def _current_gen(settings, owner_id: str) -> int:
    with store.connect(settings.db_path) as conn:
        return store.get_session_generation(conn, owner_id)


def _legacy_token(owner_id: str, *, secret: str, ttl_seconds: int = 3600) -> str:
    """Token no formato anterior a este PR -- payload SEM o campo "gen",
    assinado exatamente como `auth.sign_session` assina."""
    payload = json.dumps(
        {"owner_id": owner_id, "exp": int(time.time()) + ttl_seconds},
        separators=(",", ":"),
    ).encode("utf-8")
    payload_b64 = auth._b64e(payload)
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{auth._b64e(sig)}"


def _assert_cookie_cleared(r) -> None:
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    set_cookie = r.headers["set-cookie"]
    assert "krewhub_session=" in set_cookie
    assert "Max-Age=0" in set_cookie


@pytest.fixture
def infra(monkeypatch):
    """k8s/kirocrew mockados, registrando cada chamada -- deixa os testes
    afirmarem que revoke/teardown/emissao de token NAO rodaram (ou
    rodaram com namespace/slug reais, nunca vazios)."""
    calls: dict[str, list] = {"revoke": [], "teardown": [], "issue_token": []}

    def _revoke(_c, *, namespace, slug):
        calls["revoke"].append({"namespace": namespace, "slug": slug})
        return "ok"

    def _teardown(_c, *, namespace, slug):
        calls["teardown"].append({"namespace": namespace, "slug": slug})
        return {"namespace": namespace, "slug": slug, "steps": {}}

    def _issue_token_url(_c, *, namespace, slug, host, public_port, scheme="http", ttl="24h"):
        calls["issue_token"].append({"namespace": namespace, "slug": slug})
        return f"{scheme}://{host}:{public_port}/?token=fake"

    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    monkeypatch.setattr(session_client, "revoke_session", _revoke)
    monkeypatch.setattr(k8s_manager, "teardown_dev_workload", _teardown)
    monkeypatch.setattr(session_client, "issue_token_url", _issue_token_url)
    return calls


# ---------------------------------------------------------------------------
# store.py
# ---------------------------------------------------------------------------


def test_get_session_generation_defaults_to_zero_when_never_set(settings):
    with store.connect(settings.db_path) as conn:
        assert store.get_session_generation(conn, "dev-never-seen@test.local") == 0


def test_bump_session_generation_increments_and_persists(settings):
    with store.connect(settings.db_path) as conn:
        assert store.bump_session_generation(conn, OWNER_A) == 1
        assert store.bump_session_generation(conn, OWNER_A) == 2
    with store.connect(settings.db_path) as conn:
        assert store.get_session_generation(conn, OWNER_A) == 2


def test_bump_session_generation_never_creates_a_devs_row(settings):
    """Regressao: o contador morava em `devs`, e o bump criava uma linha
    placeholder la -- que todos os guards de "nao provisionado"
    (`row is None`) passavam a enxergar como provisionado."""
    with store.connect(settings.db_path) as conn:
        store.bump_session_generation(conn, OWNER_A)
        assert store.get(conn, OWNER_A) is None


def test_bump_session_generation_is_per_owner(settings):
    with store.connect(settings.db_path) as conn:
        store.bump_session_generation(conn, OWNER_A)
        assert store.get_session_generation(conn, OWNER_B) == 0


# ---------------------------------------------------------------------------
# auth.py
# ---------------------------------------------------------------------------


def test_verify_session_payload_includes_gen():
    token = auth.sign_session(OWNER_A, secret="s", ttl_seconds=60, gen=3)
    payload = auth.verify_session_payload(token, secret="s")
    assert payload == {"owner_id": OWNER_A, "exp": mock.ANY, "gen": 3}


def test_verify_session_still_returns_plain_owner_id_unaffected_by_gen():
    """Contrato antigo (usado por tests/test_auth_tokens.py) continua o
    mesmo -- so owner_id, sem exigir SQLite."""
    token = auth.sign_session(OWNER_A, secret="s", ttl_seconds=60, gen=7)
    assert auth.verify_session(token, secret="s") == OWNER_A


def test_verify_session_payload_rejects_boolean_gen():
    """`True` e subclasse de `int` em Python -- `isinstance` aceitaria
    `"gen": true` como geracao 1."""
    token = auth.sign_session(OWNER_A, secret="s", ttl_seconds=60, gen=True)
    with pytest.raises(auth.AuthTokenError):
        auth.verify_session_payload(token, secret="s")


def test_boolean_gen_token_rejected_when_current_generation_is_one(client, settings):
    _provision(settings, OWNER_A)
    with store.connect(settings.db_path) as conn:
        assert store.bump_session_generation(conn, OWNER_A) == 1
    token = auth.sign_session(
        OWNER_A, secret=settings.session_secret, ttl_seconds=3600, gen=True
    )
    r = client.get(f"/devs/{_path(OWNER_A)}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_legacy_token_without_gen_is_accepted_at_generation_zero_and_rejected_after_bump(
    client, settings
):
    _provision(settings, OWNER_A)
    legacy = _legacy_token(OWNER_A, secret=settings.session_secret)
    assert "gen" not in json.loads(auth._b64d(legacy.split(".", 1)[0]))
    headers = {"Authorization": f"Bearer {legacy}"}

    assert client.get(f"/devs/{_path(OWNER_A)}", headers=headers).status_code == 200

    with store.connect(settings.db_path) as conn:
        store.bump_session_generation(conn, OWNER_A)
    assert client.get(f"/devs/{_path(OWNER_A)}", headers=headers).status_code == 401


# ---------------------------------------------------------------------------
# Verificacao de geracao nas rotas protegidas
# ---------------------------------------------------------------------------


def test_token_signed_before_generation_bump_is_rejected(client, sign_cookie, settings):
    """Nivel de verificacao (bump direto no store, sem passar por
    /logout) -- o ponta a ponta via /logout real esta em
    `test_logout_endpoint_revokes_the_token_it_was_called_with`."""
    stale_token = sign_cookie(OWNER_A)  # gen=0, igual a geracao atual

    with store.connect(settings.db_path) as conn:
        store.bump_session_generation(conn, OWNER_A)

    client.cookies.set("krewhub_session", stale_token)
    r = client.get(f"/devs/{_path(OWNER_A)}")
    assert r.status_code == 401
    assert "revogada" in r.json()["detail"]


def test_freshly_signed_token_after_bump_still_works(client, sign_cookie, settings):
    _provision(settings, OWNER_A)
    with store.connect(settings.db_path) as conn:
        new_gen = store.bump_session_generation(conn, OWNER_A)

    client.cookies.set("krewhub_session", sign_cookie(OWNER_A, gen=new_gen))
    r = client.get(f"/devs/{_path(OWNER_A)}")
    # 200 com o registro real -- nao 401/403 -- prova que a credencial
    # POS-bump (geracao correta) foi aceita normalmente.
    assert r.status_code == 200
    assert r.json()["slug"] == "dev-a-test-local"


@pytest.mark.parametrize("accept", [None, "text/html"])
def test_root_with_revoked_token_redirects_to_login(client, sign_cookie, settings, accept):
    stale_token = sign_cookie(OWNER_A)
    with store.connect(settings.db_path) as conn:
        store.bump_session_generation(conn, OWNER_A)

    headers = {"Accept": accept} if accept else {}
    client.cookies.set("krewhub_session", stale_token)
    r = client.get("/", headers=headers, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert r.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# /logout de ponta a ponta
# ---------------------------------------------------------------------------


def test_logout_endpoint_revokes_the_token_it_was_called_with(client, sign_cookie, settings, infra):
    _provision(settings, OWNER_A)
    token = sign_cookie(OWNER_A)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get(f"/devs/{_path(OWNER_A)}", headers=headers).status_code == 200

    _assert_cookie_cleared(client.get("/logout", headers=headers, follow_redirects=False))

    assert _current_gen(settings, OWNER_A) == 1
    r = client.get(f"/devs/{_path(OWNER_A)}", headers=headers)
    assert r.status_code == 401
    assert "revogada" in r.json()["detail"]


@pytest.mark.parametrize("provisioned", [False, True], ids=["never-provisioned", "provisioned"])
def test_logout_bumps_generation_so_a_second_stale_tab_stops_working(
    client, sign_cookie, settings, infra, provisioned
):
    """Duas 'abas' (TestClients separados, cada um com seu cookie jar)
    com o MESMO token valido; uma chama /logout, a outra deixa de
    conseguir usar o token antigo -- sem precisar de nenhum estado alem
    do que /logout ja grava no SQLite."""
    import app.main as main

    if provisioned:
        _provision(settings, OWNER_A)
    shared_token = sign_cookie(OWNER_A)

    with TestClient(main.app, base_url="http://krewhub.kiro.internal") as tab2:
        tab2.cookies.set("krewhub_session", shared_token)
        before = tab2.get(f"/devs/{_path(OWNER_A)}")
        assert before.status_code == (200 if provisioned else 404)

        client.cookies.set("krewhub_session", shared_token)
        _assert_cookie_cleared(client.get("/logout", follow_redirects=False))

        after = tab2.get(f"/devs/{_path(OWNER_A)}")
        assert after.status_code == 401

    assert len(infra["teardown"]) == (1 if provisioned else 0)


def test_logout_of_never_provisioned_owner_keeps_not_provisioned_routes_at_404(
    client, sign_cookie, settings, infra
):
    """Regressao: com o contador em `devs`, o bump do /logout criava uma
    linha placeholder (slug/namespace vazios) e, apos re-login, GET
    /devs/{owner} passava a devolver 200, /close chamava revoke/teardown
    com namespace='' e /session tentava emitir token contra slug=''."""
    client.cookies.set("krewhub_session", sign_cookie(OWNER_A))
    _assert_cookie_cleared(client.get("/logout", follow_redirects=False))
    assert _current_gen(settings, OWNER_A) == 1

    # "Re-login": token novo com a geracao atual (mesmo que /callback emite).
    client.cookies.set("krewhub_session", sign_cookie(OWNER_A, gen=1))

    assert client.get(f"/devs/{_path(OWNER_A)}").status_code == 404
    assert client.get("/close").status_code == 404
    assert client.post(f"/devs/{_path(OWNER_A)}/session").status_code == 404
    assert infra == {"revoke": [], "teardown": [], "issue_token": []}


@pytest.mark.parametrize("failing", ["get_clients", "teardown_dev_workload"])
def test_logout_bumps_generation_even_when_teardown_raises_unexpected_exception(
    client, sign_cookie, settings, infra, monkeypatch, failing
):
    """Infra indisponivel (ex.: kubeconfig quebrado -> RuntimeError em
    get_clients) nao pode deixar o token antigo valido: o bump roda
    ANTES do teardown melhor-esforco."""
    _provision(settings, OWNER_A)
    gen_seen_by_k8s_call = []

    def _raise(*_a, **_kw):
        gen_seen_by_k8s_call.append(_current_gen(settings, OWNER_A))
        raise RuntimeError("cluster indisponivel")

    monkeypatch.setattr(k8s_manager, failing, _raise)
    token = sign_cookie(OWNER_A)
    client.cookies.set("krewhub_session", token)

    _assert_cookie_cleared(client.get("/logout", follow_redirects=False))

    assert gen_seen_by_k8s_call == [1]  # bump ja tinha rodado
    assert _current_gen(settings, OWNER_A) == 1
    r = client.get(f"/devs/{_path(OWNER_A)}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_logout_with_stale_token_clears_cookie_without_bump_or_teardown(
    client, sign_cookie, settings, infra
):
    _provision(settings, OWNER_A)
    stale_token = sign_cookie(OWNER_A)
    with store.connect(settings.db_path) as conn:
        store.bump_session_generation(conn, OWNER_A)

    client.cookies.set("krewhub_session", stale_token)
    _assert_cookie_cleared(client.get("/logout", follow_redirects=False))

    assert _current_gen(settings, OWNER_A) == 1
    assert infra["revoke"] == []
    assert infra["teardown"] == []


def test_close_does_not_bump_generation(client, sign_cookie, settings, infra):
    _provision(settings, OWNER_A)
    token = sign_cookie(OWNER_A)
    client.cookies.set("krewhub_session", token)

    assert client.get("/close").status_code == 200

    assert _current_gen(settings, OWNER_A) == 0
    assert len(infra["teardown"]) == 1
    assert client.get(f"/devs/{_path(OWNER_A)}").status_code == 200


def test_logout_of_one_owner_does_not_revoke_another_owners_token(
    client, sign_cookie, settings, infra
):
    _provision(settings, OWNER_A)
    _provision(settings, OWNER_B)
    token_a = sign_cookie(OWNER_A)
    token_b = sign_cookie(OWNER_B)

    _assert_cookie_cleared(
        client.get("/logout", headers={"Authorization": f"Bearer {token_a}"}, follow_redirects=False)
    )

    assert _current_gen(settings, OWNER_A) == 1
    assert _current_gen(settings, OWNER_B) == 0
    r_b = client.get(f"/devs/{_path(OWNER_B)}", headers={"Authorization": f"Bearer {token_b}"})
    assert r_b.status_code == 200
    r_a = client.get(f"/devs/{_path(OWNER_A)}", headers={"Authorization": f"Bearer {token_a}"})
    assert r_a.status_code == 401


# ---------------------------------------------------------------------------
# /callback depois de /logout
# ---------------------------------------------------------------------------


def _callback_token(client, monkeypatch, owner_id: str) -> str:
    import app.main as main

    monkeypatch.setitem(main._pending_logins, "fake-state", "fake-verifier")
    monkeypatch.setattr(
        main,
        "exchange_code",
        lambda _settings, *, code, code_verifier: {"owner_id": owner_id, "claims": {}, "tokens": {}},
    )
    r = client.get("/callback?code=abc&state=fake-state", follow_redirects=False)
    assert r.status_code == 302
    return r.headers["set-cookie"].split("krewhub_session=")[1].split(";")[0]


def test_callback_signs_new_session_with_current_generation_not_stale_zero(
    client, settings, monkeypatch
):
    """Se um owner ja tinha feito /logout antes (geracao > 0), o
    PROXIMO /callback tem que assinar o novo token com a geracao atual
    -- nao com 0 -- senao o /logout anterior deixaria o dev
    permanentemente deslogado."""
    with store.connect(settings.db_path) as conn:
        store.bump_session_generation(conn, OWNER_A)
        store.bump_session_generation(conn, OWNER_A)

    token = _callback_token(client, monkeypatch, OWNER_A)

    payload = auth.verify_session_payload(token, secret=settings.session_secret)
    assert payload["gen"] == 2


def test_full_relogin_cycle_after_logout_issues_working_token(
    client, sign_cookie, settings, infra, monkeypatch
):
    _provision(settings, OWNER_A)
    old_token = sign_cookie(OWNER_A)
    _assert_cookie_cleared(
        client.get("/logout", headers={"Authorization": f"Bearer {old_token}"}, follow_redirects=False)
    )

    new_token = _callback_token(client, monkeypatch, OWNER_A)

    assert auth.verify_session_payload(new_token, secret=settings.session_secret)["gen"] == 1
    r_new = client.get(f"/devs/{_path(OWNER_A)}", headers={"Authorization": f"Bearer {new_token}"})
    assert r_new.status_code == 200
    r_old = client.get(f"/devs/{_path(OWNER_A)}", headers={"Authorization": f"Bearer {old_token}"})
    assert r_old.status_code == 401
