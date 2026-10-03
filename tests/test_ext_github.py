"""Extensão GitHub (pacote `extensions/github`): contribuição ao Pod, hash do
Pod, validação do host e o formulário do lobby de ponta a ponta (k8s mockado).
O comportamento do credential helper com o git real fica em
`tests/test_ext_github_git.py`."""

from __future__ import annotations

import configparser
import dataclasses
from unittest import mock

import pytest

from app import extensions, store
from app.extensions import base, runtime
from app.extensions.contributions import collect_files
from app.k8s_templates import SPEC_HASH_ANNOTATION, build_pod
from krewhub_ext_github import (
    GITCONFIG_PATH,
    TOKEN_DIR,
    TOKEN_FILE,
    GithubExtension,
    render_gitconfig,
)
from tests.ext_demo import fake_clients, pod  # noqa: F401
from tests.test_close_logout import mocked_revoke, mocked_teardown, provisioned  # noqa: F401
from tests.test_k8s_manager import _settings

OWNER = "dev-a@test.local"
URL = "/devs/dev-a%40test.local"
SLUG = "dev-a-test-local"
TOKEN = "github_pat_SUPER_SECRET_VALUE"


def _contribution(config=None):
    ctx = base.BuildContext(OWNER, SLUG, "ns", config or {}, None)
    return GithubExtension().pod_contribution(ctx)


def _pod(config=None, contribution=None):
    return build_pod("ns", SLUG, _settings(), [("github", contribution or _contribution(config))])


# --- definição e validação ----------------------------------------------------


def test_entry_point_is_discovered_and_definition_is_valid():
    found = extensions.discover(refresh=True)
    assert isinstance(found["github"], GithubExtension)
    found["github"].check_definition()


def test_fields_are_a_required_secret_token_and_an_optional_host():
    fields = GithubExtension().field_map()
    assert fields["token"].kind == "secret" and fields["token"].required
    assert fields["host"].default == "github.com" and not fields["host"].required


@pytest.mark.parametrize(
    "host", ["github.com", "git.example.com", "ghe.corp.example.org:8443", "GitHub.com", "a", "10.0.0.5"]
)
def test_valid_hosts_are_accepted(host):
    assert GithubExtension().validate({"host": host}) == []


@pytest.mark.parametrize(
    "host",
    [
        "github.com\n[core]\n\tsshCommand = evil",
        'a"b.com',
        "github.com/path",
        "https://github.com",
        "user@github.com",
        "gith ub.com",
        "-bad.com",
        "bad-.com",
        "a..b",
        "host:port",
        "host:123456",
        "github.com]\n[x",
        "$(id).com",
    ],
)
def test_hosts_that_could_break_out_of_the_gitconfig_are_rejected(host):
    assert GithubExtension().validate({"host": host}) == ["Servidor: formato inválido"]


def test_empty_host_means_the_default():
    assert GithubExtension().validate({}) == []
    assert 'credential "https://github.com"' in _contribution({}).files["gitconfig"]
    assert 'credential "https://github.com"' in _contribution({"host": "  "}).files["gitconfig"]


# --- contribuição -------------------------------------------------------------


def test_contribution_is_declarative_only_with_no_sidecar_env_or_tools():
    c = _contribution()
    assert c.containers == [] and c.init_containers == [] and c.tools is None
    assert c.main_env == [] and c.skills == {}


def test_token_is_a_read_only_secret_file_not_an_env_var():
    c = _contribution()
    (vol,) = c.volumes
    assert vol["secret"]["secretName"] == f"krewhub-ext-{SLUG}"
    assert vol["secret"]["items"] == [{"key": "github.token", "path": "token"}]
    assert vol["secret"]["optional"] is True  # o Pod sobe mesmo se a chave ainda não existir
    mounts = {m["mountPath"]: m for m in c.main_volume_mounts}
    assert mounts[TOKEN_DIR] == {"name": vol["name"], "mountPath": TOKEN_DIR, "readOnly": True}
    assert TOKEN_FILE == f"{TOKEN_DIR}/token"


def test_gitconfig_is_mounted_read_only_at_etc_gitconfig_through_the_files_volume():
    c = _contribution()
    mount = next(m for m in c.main_volume_mounts if m["mountPath"] == GITCONFIG_PATH)
    assert mount == {
        "name": base.files_volume_name("github"),
        "mountPath": "/etc/gitconfig",
        "subPath": "gitconfig",
        "readOnly": True,
    }
    assert set(c.files) == {"gitconfig"}


def test_gitconfig_parses_and_points_the_helper_at_the_token_file_only():
    text = render_gitconfig("git.example.com:8443")
    cp = configparser.RawConfigParser()
    cp.read_string(text)
    assert cp.sections() == ['credential "https://git.example.com:8443"']
    helper = cp.get(cp.sections()[0], "helper")
    assert helper.startswith('"!') or helper.startswith("!")
    assert TOKEN_FILE in text and "username=x-access-token" in text
    assert "GITHUB_TOKEN" not in text and "ghp_" not in text


def test_host_is_lowercased_into_the_gitconfig():
    assert 'credential "https://github.example.com"' in _contribution({"host": "GitHub.Example.com"}).files["gitconfig"]


def test_contribution_merges_into_the_pod_without_touching_the_main_env():
    p = _pod()
    main = p["spec"]["containers"][0]
    assert not [e for e in main.get("env", []) if "GITHUB" in e["name"] or "TOKEN" in e["name"]]
    assert {m["mountPath"] for m in main["volumeMounts"]} >= {TOKEN_DIR, GITCONFIG_PATH}
    names = {v["name"] for v in p["spec"]["volumes"]}
    assert {"github-token", "github-files"} <= names
    assert len(p["spec"]["containers"]) == 1  # nenhum sidecar


def test_pod_contains_no_token_value_anywhere():
    text = str(_pod()) + str(collect_files([("github", _contribution())]))
    assert TOKEN not in text


# --- hash do Pod --------------------------------------------------------------


def _hash(config):
    return _pod(config)["metadata"]["annotations"][SPEC_HASH_ANNOTATION]


def test_pod_hash_is_stable_and_follows_the_host_but_not_the_token():
    assert _hash({}) == _hash({"host": "github.com"}) == _hash({"host": "GitHub.com"})
    assert _hash({}) != _hash({"host": "git.example.com"})
    # o token não entra na contribuição: trocá-lo não recria o Pod (o kubelet atualiza o arquivo)
    assert _hash({"token": "a"}) == _hash({"token": "b"})


# --- status -------------------------------------------------------------------


def test_status_is_ready_and_names_the_host_without_a_secret():
    ext = GithubExtension()
    ctx = base.ExtensionContext(
        OWNER, SLUG, "ns", "kirocrew-x", {"host": "git.example.com"}, {}, {"pod.ready": True}, None, "github",
        lambda c, s: "", lambda k: None,
    )
    st = ext.status(ctx)
    assert st.state == "ready" and "git.example.com" in st.card.summary
    assert TOKEN not in str(st)


def _existing_secret(fake_clients):  # noqa: F811
    fake_clients.core.read_namespaced_secret.side_effect = None
    secret = mock.Mock()
    secret.data = {"github.token": "eA=="}
    fake_clients.core.read_namespaced_secret.return_value = secret


# --- lobby ponta a ponta ------------------------------------------------------


@pytest.fixture
def gh_client(client, settings, monkeypatch, fake_clients, sign_cookie):  # noqa: F811
    import app.main as main
    from app import chp_client, k8s_manager, kiro_login, session_client

    extensions.reset_for_tests()
    s = dataclasses.replace(settings, extensions_enabled="github")
    monkeypatch.setattr(main, "_settings", s)
    plans: list = []

    def _reconcile(_s, owner_id, plans_=()):
        plans.append(plans_)
        slug = main.tpl.slugify(owner_id)
        return {"owner_id": owner_id, "slug": slug, "namespace": _s.dev_namespace,
                "host": f"{slug}.kiro.internal", "steps": {}}

    monkeypatch.setattr(k8s_manager, "reconcile_dev", _reconcile)
    monkeypatch.setattr(k8s_manager, "wait_for_ready", lambda *a, **kw: True)
    monkeypatch.setattr(chp_client, "register_route", lambda *a, **kw: {"status": 201})
    monkeypatch.setattr(session_client, "issue_token_url", lambda *a, **kw: "http://x/?token=t")
    monkeypatch.setattr(
        kiro_login, "start_device_flow",
        lambda *a, **kw: {"already_logged_in": False, "verification_url": "https://idp.test/d", "user_code": "AB-1"},
    )
    monkeypatch.setattr(runtime, "on_pod_ready_best_effort", lambda *a, **kw: None)
    client.cookies.set("krewhub_session", sign_cookie(OWNER))
    yield client, s, plans
    extensions.reset_for_tests()


FORM = {
    "login_mode": "personal",
    "ext.github.enabled": "on",
    "ext.github.host": "git.example.com",
    "ext.github.token": TOKEN,
}


def test_lobby_shows_a_write_only_token_field_and_the_host(gh_client):
    client, _, _ = gh_client
    html = client.get(f"{URL}/lobby").text
    assert 'name="ext.github.token"' in html and 'name="ext.github.host"' in html
    token_input = html.split('name="ext.github.token"')[1].split(">")[0]
    assert 'value=""' in token_input and 'autocomplete="new-password"' in token_input
    assert TOKEN not in html


def test_saving_the_form_sends_the_token_only_to_the_k8s_secret(gh_client, fake_clients):  # noqa: F811
    client, s, plans = gh_client
    r = client.post(f"{URL}/lobby", data=FORM)
    assert r.status_code == 200 and TOKEN not in r.text
    assert fake_clients.core.create_namespaced_secret.call_args.args[1]["stringData"] == {"github.token": TOKEN}

    (plan_set,) = plans
    assert [p.ext_id for p in plan_set] == ["github"]
    assert TOKEN not in str(plan_set)

    with store.connect(s.db_path) as conn:
        row = store.get_extension(conn, OWNER, "github")
        dump = "\n".join(conn.iterdump())
    assert row["enabled"] and row["config"] == {"host": "git.example.com"}
    assert row["secrets"]["token"]["set"] is True
    assert TOKEN not in dump and TOKEN not in str(row)

    html = client.get(f"{URL}/lobby").text
    assert TOKEN not in html


def test_blank_token_on_a_later_save_keeps_the_stored_one(gh_client, fake_clients):  # noqa: F811
    client, s, _ = gh_client
    client.post(f"{URL}/lobby", data=FORM)
    fake_clients.core.create_namespaced_secret.reset_mock()
    fake_clients.core.patch_namespaced_secret.reset_mock()
    r = client.post(f"{URL}/lobby", data={**FORM, "ext.github.token": ""})
    assert r.status_code == 200
    fake_clients.core.create_namespaced_secret.assert_not_called()
    fake_clients.core.patch_namespaced_secret.assert_not_called()


def test_rotating_the_token_patches_the_secret_and_leaves_the_pod_spec_unchanged(gh_client, fake_clients):  # noqa: F811
    client, s, _ = gh_client
    client.post(f"{URL}/lobby", data=FORM)
    before = _hash({"host": "git.example.com"})
    _existing_secret(fake_clients)
    client.post(f"{URL}/lobby", data={**FORM, "ext.github.token": "github_pat_NEW"})
    patch = fake_clients.core.patch_namespaced_secret.call_args.args[2]
    assert patch == {"stringData": {"github.token": "github_pat_NEW"}}
    assert _hash({"host": "git.example.com"}) == before


def test_first_save_without_a_token_is_rejected(gh_client, fake_clients):  # noqa: F811
    client, s, plans = gh_client
    r = client.post(f"{URL}/lobby", data={**FORM, "ext.github.token": ""})
    assert r.status_code == 400 and "Token do GitHub: obrigatório" in r.text
    assert plans == []
    fake_clients.core.create_namespaced_secret.assert_not_called()


def test_invalid_host_is_rejected_and_nothing_is_saved(gh_client, fake_clients):  # noqa: F811
    client, s, plans = gh_client
    r = client.post(f"{URL}/lobby", data={**FORM, "ext.github.host": "a\n[core]\nx = y"})
    assert r.status_code == 400 and "Servidor: formato inválido" in r.text
    assert plans == []
    fake_clients.core.create_namespaced_secret.assert_not_called()
    with store.connect(s.db_path) as conn:
        assert store.get_extension(conn, OWNER, "github") is None


def test_card_is_ready_once_the_pod_is_up_and_has_no_actions(gh_client, fake_clients):  # noqa: F811
    client, s, _ = gh_client
    client.post(f"{URL}/lobby", data=FORM)
    fake_clients.core.read_namespaced_pod.side_effect = None
    fake_clients.core.read_namespaced_pod.return_value = pod(sidecars=())
    (view,) = runtime.evaluate(s, OWNER)
    assert view.state == "ready" and view.actions == ()
    html = client.get(f"{URL}/extensions/cards").text
    assert "git.example.com" in html and TOKEN not in html


def test_disabling_the_extension_wipes_its_key_from_the_secret(gh_client, fake_clients):  # noqa: F811
    client, s, _ = gh_client
    client.post(f"{URL}/lobby", data=FORM)
    _existing_secret(fake_clients)
    client.post(f"{URL}/lobby", data={"login_mode": "personal"})
    patch = fake_clients.core.patch_namespaced_secret.call_args.args[2]
    assert patch == {"data": {"github.token": None}}


def test_logout_wipes_the_github_token_with_the_rest_of_the_extension_keys(
    gh_client, mocked_revoke, mocked_teardown, provisioned, fake_clients, monkeypatch  # noqa: F811
):
    from app import k8s_manager

    # mocked_teardown troca get_clients por um Mock genérico; o Secret precisa ser o de fake_clients
    monkeypatch.setattr(k8s_manager, "get_clients", lambda _s: fake_clients)
    client, s, _ = gh_client
    with store.connect(s.db_path) as conn:
        store.upsert_extension(
            conn, OWNER, "github", enabled=True, config={}, secrets={"token": store.secret_marker()}
        )
    _existing_secret(fake_clients)
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert fake_clients.core.patch_namespaced_secret.call_args.args[2] == {"data": {"github.token": None}}
    fake_clients.core.delete_namespaced_secret.assert_not_called()
