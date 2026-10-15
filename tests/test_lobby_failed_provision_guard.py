"""Regressão do issue #6 -- POST /devs/{owner_id}/lobby persiste a escolha
(store.set_login_choice) ANTES do provision. Pra um owner sem linha `devs`,
isso insere uma linha PLACEHOLDER com namespace/slug/host vazios e status
'lobby_pending'. Se reconcile_dev falha, a linha fica com esses valores vazios.

Antes do fix, todo guard de "não provisionado" checava só `store.get(...) is
None`, então os endpoints tratavam o owner como provisionado e agiam sobre
namespace='', slug='', host='' (p.ex. issue_token_url gerando
http://:8080/?token=...). O fix: `store.get_provisioned` trata a linha
placeholder (namespace vazio) como não provisionada, e os guards de
`_close_dev_session`, `new_session`, `kiro_login_start` e `get_dev` passam a
usá-lo -- devolvendo 404 e SEM nenhuma chamada ao Kubernetes.

k8s/CHP/kirocrew 100% mockados (suite offline, ver conftest.py)."""

from __future__ import annotations

from unittest import mock

import pytest
from starlette.testclient import TestClient

from app import k8s_manager, kiro_login, session_client, store
from tests.ext_demo import lobby_data

OWNER = "dev-a@test.local"
OWNER_ENC = "dev-a%40test.local"


@pytest.fixture
def noraise_client(settings, monkeypatch):
    """Como o fixture `client`, mas com raise_server_exceptions=False: uma
    exceção não-tratada em reconcile_dev vira o 500 real que o FastAPI
    devolve em produção, em vez de ser re-levantada pelo TestClient."""
    import app.main as main

    monkeypatch.setattr(main, "_settings", settings)
    monkeypatch.setattr(main, "_pending_logins", {})
    with TestClient(
        main.app, base_url="http://krewhub.kiro.internal", raise_server_exceptions=False
    ) as c:
        yield c


@pytest.fixture
def failing_reconcile(monkeypatch):
    """reconcile_dev levanta (como se o API server do k8s respondesse erro).
    get_clients devolve um Mock que registramos pra provar que NENHUM endpoint
    pós-falha chega a instanciar um cliente k8s."""

    def _boom(*_a, **_kw):
        raise RuntimeError("API server indisponível (simulado)")

    get_clients = mock.Mock(name="get_clients", return_value=mock.Mock(name="k8s_clients"))

    monkeypatch.setattr(k8s_manager, "reconcile_dev", _boom)
    monkeypatch.setattr(k8s_manager, "get_clients", get_clients)
    # Se qualquer guard vazar e seguir em frente, estes seriam chamados com
    # namespace/slug/host vazios -- fazemos cada um explodir pra o teste
    # falhar ALTO (não silenciosamente 200) na ausência do fix.
    monkeypatch.setattr(
        session_client, "issue_token_url",
        mock.Mock(side_effect=AssertionError("issue_token_url não devia rodar numa linha placeholder")),
    )
    monkeypatch.setattr(
        session_client, "revoke_session",
        mock.Mock(side_effect=AssertionError("revoke_session não devia rodar numa linha placeholder")),
    )
    monkeypatch.setattr(
        k8s_manager, "teardown_dev_workload",
        mock.Mock(side_effect=AssertionError("teardown_dev_workload não devia rodar numa linha placeholder")),
    )
    monkeypatch.setattr(
        kiro_login, "start_device_flow",
        mock.Mock(side_effect=AssertionError("start_device_flow não devia rodar numa linha placeholder")),
    )
    return get_clients


def _place_placeholder_row(client, failing_reconcile, sign_cookie, settings):
    """Reproduz o cenário do issue: POST /lobby cujo reconcile_dev falha,
    deixando a linha placeholder no banco. Retorna a resposta do POST."""
    client.cookies.set("krewhub_session", sign_cookie(OWNER))
    r = client.post(f"/devs/{OWNER_ENC}/lobby", data=lobby_data({"login_mode": "personal"}))
    # Sanidade: o POST falha (500) e a linha placeholder existe com namespace vazio.
    assert r.status_code == 500
    with store.connect(settings.db_path) as conn:
        row = store.get(conn, OWNER)
    assert row is not None
    assert row["namespace"] == ""
    assert row["status"] == "lobby_pending"
    assert row["login_mode"] == "personal"
    return r


def test_failed_lobby_provision_leaves_placeholder_row(noraise_client, failing_reconcile, sign_cookie, settings):
    """O setup do cenário é válido: a falha deixa a linha placeholder."""
    _place_placeholder_row(noraise_client, failing_reconcile, sign_cookie, settings)


def test_get_dev_is_404_on_placeholder_row(noraise_client, failing_reconcile, sign_cookie, settings):
    _place_placeholder_row(noraise_client, failing_reconcile, sign_cookie, settings)
    r = noraise_client.get(f"/devs/{OWNER_ENC}")
    assert r.status_code == 404
    # get_dev não toca no cluster de qualquer forma, mas garantimos o 404.


def test_close_is_404_on_placeholder_row_and_makes_no_k8s_calls(noraise_client, failing_reconcile, sign_cookie, settings):
    _place_placeholder_row(noraise_client, failing_reconcile, sign_cookie, settings)
    failing_reconcile.reset_mock()  # ignora chamadas do setup (o POST falhou antes de get_clients)
    r = noraise_client.get("/close")  # /close resolve o owner pelo cookie (require_session)
    assert r.status_code == 404
    failing_reconcile.assert_not_called()  # nenhum cliente k8s instanciado


def test_new_session_is_404_on_placeholder_row_and_makes_no_k8s_calls(noraise_client, failing_reconcile, sign_cookie, settings):
    _place_placeholder_row(noraise_client, failing_reconcile, sign_cookie, settings)
    failing_reconcile.reset_mock()
    r = noraise_client.post(f"/devs/{OWNER_ENC}/session")
    assert r.status_code == 404
    failing_reconcile.assert_not_called()


def test_kiro_login_is_404_on_placeholder_row_and_makes_no_k8s_calls(noraise_client, failing_reconcile, sign_cookie, settings):
    _place_placeholder_row(noraise_client, failing_reconcile, sign_cookie, settings)
    failing_reconcile.reset_mock()
    r = noraise_client.post(f"/devs/{OWNER_ENC}/kiro-login?mode=personal")
    assert r.status_code == 404
    failing_reconcile.assert_not_called()


def test_lobby_form_still_skipped_with_saved_choices_despite_placeholder(noraise_client, failing_reconcile, sign_cookie, settings):
    """Acceptance: GET /lobby ainda pula o form com as escolhas salvas.

    A linha placeholder guarda login_mode='personal'; o lobby_form NÃO usa
    get_provisioned (de propósito), então segue lendo a escolha e pulando o
    form. Como o reconcile está mockado pra falhar, o GET também é 500 -- mas
    o que importa é que ele NÃO mostra o form (não voltou a tratar como
    primeira vez)."""
    _place_placeholder_row(noraise_client, failing_reconcile, sign_cookie, settings)
    r = noraise_client.get(f"/devs/{OWNER_ENC}/lobby")
    assert "<form" not in r.text  # pulou o form -> re-tentou provisionar com a escolha salva

