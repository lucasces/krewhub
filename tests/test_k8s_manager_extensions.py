"""Secret/ConfigMap das extensões e plano de reconcile (k8s mockado)."""

from __future__ import annotations

from unittest import mock

import pytest
from kubernetes.client.rest import ApiException

from app import k8s_manager
from app.extensions.base import PodContribution
from app.extensions.contributions import ExtPlan
from tests.test_k8s_manager import _not_found, _settings, fake_clients  # noqa: F401


def _secret(data: dict) -> mock.Mock:
    s = mock.Mock()
    s.data = {k: "dg==" for k in data}
    return s


def test_ensure_ext_secret_creates_with_generated_and_set_values(fake_clients):
    status = k8s_manager.ensure_ext_secret(
        fake_clients,
        "ns",
        "alice",
        set_values={"demo.tok": "abc"},
        generated=("demo.auth",),
        generate=lambda: "GEN",
    )
    assert status == "created"
    ns, body = fake_clients.core.create_namespaced_secret.call_args.args
    assert ns == "ns"
    assert body["metadata"]["name"] == "krewhub-ext-alice"
    assert body["metadata"]["labels"]["krewhub.pespa.net/owner-slug"] == "alice"
    assert body["stringData"] == {"demo.tok": "abc", "demo.auth": "GEN"}


def test_ensure_ext_secret_keeps_existing_generated_value(fake_clients):
    fake_clients.core.read_namespaced_secret.side_effect = None
    fake_clients.core.read_namespaced_secret.return_value = _secret({"demo.auth": 1})
    status = k8s_manager.ensure_ext_secret(
        fake_clients, "ns", "alice", generated=("demo.auth",), generate=lambda: "NEW"
    )
    assert status == "unchanged"
    fake_clients.core.patch_namespaced_secret.assert_not_called()


def test_ensure_ext_secret_patches_only_missing_generated_and_set(fake_clients):
    fake_clients.core.read_namespaced_secret.side_effect = None
    fake_clients.core.read_namespaced_secret.return_value = _secret({"demo.auth": 1})
    status = k8s_manager.ensure_ext_secret(
        fake_clients,
        "ns",
        "alice",
        set_values={"demo.tok": "abc"},
        generated=("demo.auth", "demo.other"),
        generate=lambda: "G",
    )
    assert status == "updated"
    name, ns, body = fake_clients.core.patch_namespaced_secret.call_args.args
    assert (name, ns) == ("krewhub-ext-alice", "ns")
    assert body == {"stringData": {"demo.tok": "abc", "demo.other": "G"}}


def test_wipe_ext_secret_keys_uses_merge_patch_with_null(fake_clients):
    fake_clients.core.read_namespaced_secret.side_effect = None
    fake_clients.core.read_namespaced_secret.return_value = _secret({"demo.a": 1, "demo.b": 1, "x.c": 1})
    wiped = k8s_manager.wipe_ext_secret_keys(fake_clients, "ns", "alice", ["demo.a", "demo.zzz"])
    assert wiped == ["demo.a"]
    call = fake_clients.core.patch_namespaced_secret.call_args
    assert call.args == ("krewhub-ext-alice", "ns", {"data": {"demo.a": None}})
    assert call.kwargs == {"_content_type": "application/merge-patch+json"}
    fake_clients.core.delete_namespaced_secret.assert_not_called()


def test_wipe_ext_secret_keys_none_wipes_everything(fake_clients):
    fake_clients.core.read_namespaced_secret.side_effect = None
    fake_clients.core.read_namespaced_secret.return_value = _secret({"demo.a": 1, "x.c": 1})
    assert k8s_manager.wipe_ext_secret_keys(fake_clients, "ns", "alice") == ["demo.a", "x.c"]
    body = fake_clients.core.patch_namespaced_secret.call_args.args[2]
    assert body == {"data": {"demo.a": None, "x.c": None}}


def test_wipe_ext_secret_keys_missing_secret_or_nothing_to_wipe(fake_clients):
    assert k8s_manager.wipe_ext_secret_keys(fake_clients, "ns", "alice") == []
    fake_clients.core.read_namespaced_secret.side_effect = None
    fake_clients.core.read_namespaced_secret.return_value = _secret({})
    assert k8s_manager.wipe_ext_secret_keys(fake_clients, "ns", "alice") == []
    fake_clients.core.patch_namespaced_secret.assert_not_called()


def test_wipe_ext_secret_keys_propagates_real_errors(fake_clients):
    fake_clients.core.read_namespaced_secret.side_effect = ApiException(status=403, reason="Forbidden")
    with pytest.raises(ApiException):
        k8s_manager.wipe_ext_secret_keys(fake_clients, "ns", "alice")


def test_ensure_ext_files_configmap_lifecycle(fake_clients):
    assert k8s_manager.ensure_ext_files_configmap(fake_clients, "ns", "alice", {}) == "absent"
    assert (
        k8s_manager.ensure_ext_files_configmap(fake_clients, "ns", "alice", {"demo.a": "1"}) == "created"
    )
    body = fake_clients.core.create_namespaced_config_map.call_args.args[1]
    assert body["metadata"]["name"] == "krewhub-ext-files-alice"

    existing = mock.Mock()
    existing.data = {"demo.a": "1", "demo.old": "x"}
    fake_clients.core.read_namespaced_config_map.side_effect = None
    fake_clients.core.read_namespaced_config_map.return_value = existing
    assert (
        k8s_manager.ensure_ext_files_configmap(fake_clients, "ns", "alice", {"demo.a": "2"}) == "updated"
    )
    patch = fake_clients.core.patch_namespaced_config_map.call_args
    assert patch.args[2] == {"data": {"demo.a": "2", "demo.old": None}}
    assert patch.kwargs == {"_content_type": "application/merge-patch+json"}

    assert k8s_manager.ensure_ext_files_configmap(fake_clients, "ns", "alice", {}) == "deleted"
    fake_clients.core.delete_namespaced_config_map.assert_called_once_with("krewhub-ext-files-alice", "ns")


def test_teardown_ext_resources_ignores_missing(fake_clients):
    fake_clients.core.delete_namespaced_config_map.side_effect = _not_found()
    assert k8s_manager.teardown_ext_resources(fake_clients, "ns", "alice") == "already_absent"


def test_reconcile_without_plans_has_no_extension_steps(fake_clients):
    result = k8s_manager.reconcile_dev(_settings(), "dev-a@test.local")
    assert set(result["steps"]) == {
        "namespace", "secret", "configmap", "pvc", "service", "networkpolicy", "pod",
    }
    fake_clients.core.create_namespaced_secret.assert_called_once()  # só o kiro-owner-id


def test_reconcile_with_plan_creates_ext_resources_before_the_pod(fake_clients):
    plan = ExtPlan(
        "demo",
        PodContribution(
            containers=[{"name": "demo", "image": "img", "volumeMounts": [
                {"name": "demo-files", "mountPath": "/etc/demo"}]}],
            files={"c.yaml": "k: v"},
        ),
        generated_keys=("demo.auth",),
    )
    order: list[str] = []
    fake_clients.core.create_namespaced_secret.side_effect = lambda ns, b: order.append(b["metadata"]["name"])
    fake_clients.core.create_namespaced_config_map.side_effect = lambda ns, b: order.append(b["metadata"]["name"])
    fake_clients.core.create_namespaced_pod.side_effect = lambda ns, b: order.append("pod")

    result = k8s_manager.reconcile_dev(_settings(), "dev-a@test.local", (plan,))

    assert result["steps"]["ext_secret"] == "created"
    assert result["steps"]["ext_files"] == "created"
    assert order.index("krewhub-ext-dev-a-test-local") < order.index("pod")
    assert order.index("krewhub-ext-files-dev-a-test-local") < order.index("pod")
    pod = fake_clients.core.create_namespaced_pod.call_args.args[1]
    assert [c["name"] for c in pod["spec"]["containers"]] == ["kirocrew", "demo"]
