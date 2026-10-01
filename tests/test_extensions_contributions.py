"""Merge de PodContribution em build_pod + validação de conflitos."""

from __future__ import annotations

import copy

import pytest

from app import k8s_templates as tpl
from app.extensions.base import PodContribution
from app.extensions.contributions import (
    ContributionError,
    collect_files,
    files_configmap_name,
)


def _side(name="demo", **extra):
    return {"name": name, "image": "example/side:1", **extra}


def _build(settings, *contribs):
    return tpl.build_pod("krewhub-devs", "alice", settings, list(contribs))


def test_no_contributions_is_identical_to_before(settings):
    assert tpl.build_pod("krewhub-devs", "alice", settings) == _build(settings)


def test_sidecar_volume_env_and_mount_are_merged(settings):
    contrib = PodContribution(
        containers=[
            _side(volumeMounts=[{"name": "demo-state", "mountPath": "/state"}]),
        ],
        volumes=[{"name": "demo-state", "emptyDir": {}}],
        main_env=[{"name": "DEMO_URL", "value": "http://127.0.0.1:1"}],
        main_volume_mounts=[{"name": "demo-state", "mountPath": "/demo"}],
        annotations={"demo/x": "1"},
    )
    pod = _build(settings, ("demo", contrib))
    names = [c["name"] for c in pod["spec"]["containers"]]
    assert names == ["kirocrew", "demo"]
    main = pod["spec"]["containers"][0]
    assert {"name": "DEMO_URL", "value": "http://127.0.0.1:1"} in main["env"]
    assert {"name": "demo-state", "mountPath": "/demo"} in main["volumeMounts"]
    assert {"name": "demo-state", "emptyDir": {}} in pod["spec"]["volumes"]
    assert pod["metadata"]["annotations"]["demo/x"] == "1"


def test_contribution_changes_the_spec_hash(settings):
    plain = tpl.build_pod("krewhub-devs", "alice", settings)
    with_ext = _build(settings, ("demo", PodContribution(containers=[_side()])))
    ann = tpl.SPEC_HASH_ANNOTATION
    assert plain["metadata"]["annotations"][ann] != with_ext["metadata"]["annotations"][ann]


def test_files_content_changes_the_spec_hash_but_plain_pods_keep_theirs(settings):
    def pod(content):
        contrib = PodContribution(
            containers=[_side(volumeMounts=[{"name": "demo-files", "mountPath": "/etc/demo"}])],
            files={"a.yaml": content},
        )
        return _build(settings, ("demo", contrib))["metadata"]["annotations"][tpl.SPEC_HASH_ANNOTATION]

    assert pod("one") != pod("two")
    assert pod("one") == pod("one")
    plain = tpl.build_pod("krewhub-devs", "alice", settings)
    assert plain["metadata"]["annotations"][tpl.SPEC_HASH_ANNOTATION] == tpl.spec_hash(plain["spec"])


def test_files_become_a_configmap_volume_with_items(settings):
    contrib = PodContribution(
        containers=[_side(volumeMounts=[{"name": "demo-files", "mountPath": "/etc/demo"}])],
        files={"b.yaml": "x", "a.yaml": "y"},
    )
    pod = _build(settings, ("demo", contrib))
    vol = next(v for v in pod["spec"]["volumes"] if v["name"] == "demo-files")
    assert vol["configMap"]["name"] == files_configmap_name("alice") == "krewhub-ext-files-alice"
    assert vol["configMap"]["items"] == [
        {"key": "demo.a.yaml", "path": "a.yaml"},
        {"key": "demo.b.yaml", "path": "b.yaml"},
    ]
    assert collect_files([("demo", contrib)]) == {"demo.a.yaml": "y", "demo.b.yaml": "x"}


def test_overlay_still_applies_after_contributions(settings):
    import dataclasses

    overlay = '{"pod":[{"op":"add","path":"/spec/containers/1/env","value":[{"name":"FROM_OVERLAY","value":"1"}]}]}'
    s = dataclasses.replace(settings, dev_pod_overlay_json=overlay)
    pod = _build(s, ("demo", PodContribution(containers=[_side()])))
    assert pod["spec"]["containers"][1]["env"] == [{"name": "FROM_OVERLAY", "value": "1"}]


@pytest.mark.parametrize(
    "contrib, fragment",
    [
        (PodContribution(containers=[_side("other")]), "precisa se chamar"),
        (PodContribution(containers=[_side("kirocrew")]), "precisa se chamar"),
        (PodContribution(containers=[_side(readinessProbe={"httpGet": {"port": 1}})]), "readinessProbe"),
        (PodContribution(containers=[_side(securityContext={"privileged": True})]), "privileged"),
        (PodContribution(volumes=[{"name": "demo-x", "hostPath": {"path": "/"}}]), "hostPath"),
        (PodContribution(volumes=[{"name": "home", "emptyDir": {}}]), "precisa se chamar"),
        (
            PodContribution(containers=[_side(volumeMounts=[{"name": "home", "mountPath": "/h"}])]),
            "não é da extensão",
        ),
        (
            PodContribution(containers=[_side(), _side()]),
            "duplicado",
        ),
        (PodContribution(main_env=[{"name": "PYTHONDONTWRITEBYTECODE", "value": "0"}]), "já definida"),
        (
            PodContribution(
                volumes=[{"name": "demo-v", "emptyDir": {}}],
                main_volume_mounts=[{"name": "demo-v", "mountPath": "/tmp"}],
            ),
            "mountPath",
        ),
        (
            PodContribution(main_volume_mounts=[{"name": "home", "mountPath": "/x"}]),
            "não é da extensão",
        ),
    ],
)
def test_conflicts_are_rejected(settings, contrib, fragment):
    with pytest.raises(ContributionError, match=fragment):
        _build(settings, ("demo", contrib))


def test_two_extensions_cannot_define_the_same_env(settings):
    a = PodContribution(main_env=[{"name": "SHARED", "value": "1"}])
    b = PodContribution(main_env=[{"name": "SHARED", "value": "2"}])
    with pytest.raises(ContributionError, match="SHARED"):
        _build(settings, ("aaa", a), ("bbb", b))


def test_failed_merge_does_not_leak_into_other_calls(settings):
    contrib = PodContribution(containers=[_side()])
    before = copy.deepcopy(contrib)
    _build(settings, ("demo", contrib))
    assert contrib == before
