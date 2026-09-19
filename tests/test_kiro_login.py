"""POST /devs/{owner_id}/kiro-login (camada HTTP -- validação/resolução
de parâmetros, com `kiro_login.start_device_flow` mockado) +
app/kiro_login.py (camada de baixo nível -- guard clauses, idempotência,
parsing de código/URL, com `stream`/kubectl exec mockado)."""

from __future__ import annotations

from unittest import mock

import pytest

from app import k8s_manager, kiro_login, store


# ---------------------------------------------------------------------------
# Camada HTTP: POST /devs/{owner_id}/kiro-login
# ---------------------------------------------------------------------------


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
def mocked_start_device_flow(monkeypatch):
    calls = []

    def _fake(_c, *, namespace, slug, mode, identity_provider=None, region=None):
        calls.append(
            {
                "namespace": namespace,
                "slug": slug,
                "mode": mode,
                "identity_provider": identity_provider,
                "region": region,
            }
        )
        return {"already_logged_in": False, "verification_url": "https://idp.test/device", "user_code": "ABCD-EFGH"}

    monkeypatch.setattr(kiro_login, "start_device_flow", _fake)
    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    return calls


def test_kiro_login_without_mode_is_400(client, provisioned, sign_cookie, mocked_start_device_flow):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/kiro-login")
    assert r.status_code == 400


def test_kiro_login_invalid_mode_is_400(client, provisioned, sign_cookie, mocked_start_device_flow):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/kiro-login?mode=bogus")
    assert r.status_code == 400


def test_kiro_login_org_without_identity_provider_or_region_is_400(
    client, provisioned, sign_cookie, mocked_start_device_flow
):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/kiro-login?mode=org")
    assert r.status_code == 400
    assert "identity_provider" in r.json()["detail"]


def test_kiro_login_org_with_query_params_wins_over_env_var(
    client, provisioned, sign_cookie, mocked_start_device_flow, settings, monkeypatch
):
    # Env var configurada com um valor -- a query, quando presente, tem
    # que vencer (achado a validar: não é só "usa a query se a env var
    # estiver vazia", é PRECEDÊNCIA de verdade). Settings e' frozen --
    # object.__setattr__ e' o jeito suportado de ajustar um campo depois
    # de construido, só em teste.
    object.__setattr__(settings, "kiro_identity_provider", "https://env-default.example/start")
    object.__setattr__(settings, "kiro_region", "env-region-1")

    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post(
        "/devs/dev-a%40test.local/kiro-login"
        "?mode=org&identity_provider=https://query-wins.example/start&region=query-region-1"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "org"
    assert body["verification_url"] == "https://idp.test/device"
    assert body["user_code"] == "ABCD-EFGH"

    assert len(mocked_start_device_flow) == 1
    call = mocked_start_device_flow[0]
    assert call["identity_provider"] == "https://query-wins.example/start"
    assert call["region"] == "query-region-1"


def test_kiro_login_personal_ignores_identity_provider_and_region(
    client, provisioned, sign_cookie, mocked_start_device_flow
):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post(
        "/devs/dev-a%40test.local/kiro-login"
        "?mode=personal&identity_provider=should-be-ignored&region=should-be-ignored"
    )
    assert r.status_code == 200
    call = mocked_start_device_flow[0]
    assert call["mode"] == "personal"
    assert call["identity_provider"] is None
    assert call["region"] is None


def test_kiro_login_idempotent_already_logged_in(client, provisioned, sign_cookie, monkeypatch):
    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: mock.Mock())
    monkeypatch.setattr(
        kiro_login,
        "start_device_flow",
        lambda *_a, **_kw: {"already_logged_in": True, "whoami": "logged in as dev-a"},
    )
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/kiro-login?mode=personal")
    assert r.status_code == 200
    assert r.json()["already_logged_in"] is True


def test_kiro_login_requires_provisioned_owner(client, sign_cookie, mocked_start_device_flow):
    client.cookies.set("krewhub_session", sign_cookie("dev-a@test.local"))
    r = client.post("/devs/dev-a%40test.local/kiro-login?mode=personal")
    assert r.status_code == 404


def test_kiro_login_requires_auth(client, provisioned, mocked_start_device_flow):
    r = client.post("/devs/dev-a%40test.local/kiro-login?mode=personal")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Camada de baixo nível: app/kiro_login.py (stream/kubectl exec mockado)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_clients():
    pod = mock.Mock()
    pod.metadata.name = "kirocrew-dev-a-abc"
    pod.status.phase = "Running"
    clients = mock.Mock()
    clients.core.list_namespaced_pod.return_value = mock.Mock(items=[pod])
    return clients


def test_start_device_flow_rejects_invalid_mode(fake_clients):
    with pytest.raises(kiro_login.KiroLoginError, match="mode inv"):
        kiro_login.start_device_flow(fake_clients, namespace="ns", slug="s", mode="bogus")


def test_start_device_flow_org_requires_identity_provider_and_region(fake_clients):
    with pytest.raises(kiro_login.KiroLoginError, match="identity_provider e region"):
        kiro_login.start_device_flow(fake_clients, namespace="ns", slug="s", mode="org")


def test_start_device_flow_idempotent_skips_when_already_logged_in(monkeypatch, fake_clients):
    monkeypatch.setattr(
        kiro_login, "whoami", lambda *_a, **_kw: (True, "kiro-cli: logged in as dev-a@test.local")
    )
    stream_calls = []
    monkeypatch.setattr(
        kiro_login,
        "stream",
        lambda *a, **kw: stream_calls.append(kw.get("command")) or "should not matter",
    )
    result = kiro_login.start_device_flow(fake_clients, namespace="ns", slug="dev-a-test-local", mode="personal")
    assert result == {"already_logged_in": True, "whoami": "kiro-cli: logged in as dev-a@test.local"}
    # Nenhum device-flow novo disparado -- só o exec do whoami em si
    # (dentro de `kiro_login.whoami`, aqui mockado inteiro) rodou.
    assert stream_calls == []


def test_whoami_parses_not_logged_in(monkeypatch, fake_clients):
    monkeypatch.setattr(kiro_login, "stream", lambda *a, **kw: "kiro-cli: Not logged in\n")
    logged_in, detail = kiro_login.whoami(fake_clients, namespace="ns", slug="dev-a-test-local")
    assert logged_in is False
    assert "Not logged in" in detail


def test_whoami_parses_logged_in(monkeypatch, fake_clients):
    monkeypatch.setattr(kiro_login, "stream", lambda *a, **kw: "kiro-cli: logged in as dev-a@test.local\n")
    logged_in, detail = kiro_login.whoami(fake_clients, namespace="ns", slug="dev-a-test-local")
    assert logged_in is True


def test_start_device_flow_personal_happy_path_parses_code_and_url(monkeypatch, fake_clients):
    """Log já contém a needle de cada estágio desde a primeira leitura --
    `_wait_for` retorna no primeiro poll, sem sleep real nenhum (rápido,
    determinístico)."""
    monkeypatch.setattr(kiro_login, "whoami", lambda *_a, **_kw: (False, "Not logged in"))

    log_content = (
        "Select login method: (Use with Builder ID)\n"
        "Code: ABCD-1234\n"
        "Open this URL: https://view.awsapps.com/start/#/device?user_code=ABCD-1234\n"
    )

    calls = []

    def _fake_stream(_exec_fn, _pod, _ns, **kwargs):
        calls.append(kwargs["command"])
        script = kwargs["command"][-1]
        if "cat" in script:
            return log_content
        return ""

    monkeypatch.setattr(kiro_login, "stream", _fake_stream)

    result = kiro_login.start_device_flow(fake_clients, namespace="ns", slug="dev-a-test-local", mode="personal")
    assert result["already_logged_in"] is False
    assert result["user_code"] == "ABCD-1234"
    assert result["verification_url"] == "https://view.awsapps.com/start/#/device?user_code=ABCD-1234"
    # A sequência real inclui: write driver, launch (setsid), poll do
    # menu, Enter, poll final -- várias chamadas de exec, nenhuma delas
    # tocou um pod/cluster real.
    assert len(calls) >= 3


def test_start_device_flow_org_uses_identity_provider_and_region_in_command(monkeypatch, fake_clients):
    monkeypatch.setattr(kiro_login, "whoami", lambda *_a, **_kw: (False, "Not logged in"))

    log_content = (
        "Enter Start URL: (https://myorg.awsapps.com/start)\n"
        "Enter Region: (us-east-1)\n"
        "Code: WXYZ-9876\n"
        "Open this URL: https://myorg.awsapps.com/start/#/device?user_code=WXYZ-9876\n"
    )

    launch_scripts = []

    def _fake_stream(_exec_fn, _pod, _ns, **kwargs):
        script = kwargs["command"][-1]
        if "setsid" in script:
            launch_scripts.append(script)
        if "cat" in script:
            return log_content
        return ""

    monkeypatch.setattr(kiro_login, "stream", _fake_stream)

    result = kiro_login.start_device_flow(
        fake_clients,
        namespace="ns",
        slug="dev-a-test-local",
        mode="org",
        identity_provider="https://myorg.awsapps.com/start",
        region="us-east-1",
    )
    assert result["user_code"] == "WXYZ-9876"
    assert len(launch_scripts) == 1
    assert "--identity-provider" in launch_scripts[0]
    assert "https://myorg.awsapps.com/start" in launch_scripts[0]
    assert "--region us-east-1" in launch_scripts[0] or "--region 'us-east-1'" in launch_scripts[0]
