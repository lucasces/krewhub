"""Merge de PodContribution em build_pod + validação de conflitos."""

from __future__ import annotations

import copy

import pytest

from app import k8s_templates as tpl
from app.extensions.base import PodContribution, ToolsSpec
from app.extensions.contributions import (
    DEFAULT_MAIN_PATH,
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


# --- tools: binários no kirocrew via initContainer + emptyDir -----------------


def _tools(**kw):
    return ToolsSpec(image="example/tools:1", command=("sh", "-c", "cp -dR /x/. /tools/"), **kw)


def test_tools_expand_into_init_container_volume_mount_and_path(settings):
    pod = _build(settings, ("demo", PodContribution(tools=_tools())))
    spec = pod["spec"]
    init = next(c for c in spec["initContainers"] if c["name"] == "demo-tools")
    assert init["image"] == "example/tools:1"
    assert init["command"] == ["sh", "-c", "cp -dR /x/. /tools/"]
    assert init["volumeMounts"] == [{"name": "demo-tools", "mountPath": "/tools"}]
    sc = init["securityContext"]
    assert sc["runAsNonRoot"] and sc["readOnlyRootFilesystem"] and not sc["allowPrivilegeEscalation"]
    assert sc["capabilities"] == {"drop": ["ALL"]}
    assert {"name": "demo-tools", "emptyDir": {"sizeLimit": "512Mi"}} in spec["volumes"]
    main = spec["containers"][0]
    assert {"name": "demo-tools", "mountPath": "/opt/krewhub-ext/demo", "readOnly": True} in main["volumeMounts"]
    path = next(e["value"] for e in main["env"] if e["name"] == "PATH")
    assert path.startswith("/opt/krewhub-ext/demo/bin:")
    assert path.endswith(DEFAULT_MAIN_PATH)
    # o PATH original da imagem continua inteiro no fim
    assert "/usr/local/bin" in path.split(":") and "/usr/bin" in path.split(":")


def test_tools_of_two_extensions_share_one_path_entry(settings):
    pod = _build(
        settings,
        ("aaa", PodContribution(tools=_tools())),
        ("bbb", PodContribution(tools=_tools(bin_dir="sbin"))),
    )
    envs = [e for e in pod["spec"]["containers"][0]["env"] if e["name"] == "PATH"]
    assert len(envs) == 1
    assert envs[0]["value"].startswith("/opt/krewhub-ext/aaa/bin:/opt/krewhub-ext/bbb/sbin:")
    assert [c["name"] for c in pod["spec"]["initContainers"]] == ["aaa-tools", "bbb-tools"]


def test_no_tools_means_no_path_override_and_no_init_container(settings):
    pod = _build(settings, ("demo", PodContribution(containers=[_side()])))
    assert "initContainers" not in pod["spec"]
    assert all(e["name"] != "PATH" for e in pod["spec"]["containers"][0]["env"])


def test_tools_change_the_spec_hash(settings):
    ann = tpl.SPEC_HASH_ANNOTATION

    def h(image):
        t = ToolsSpec(image=image, command=("true",))
        return _build(settings, ("demo", PodContribution(tools=t)))["metadata"]["annotations"][ann]

    assert h("example/tools:1") != h("example/tools:2")


@pytest.mark.parametrize(
    "contrib, fragment",
    [
        (PodContribution(tools=ToolsSpec(image="i", command=())), "command vazio"),
        (PodContribution(tools=ToolsSpec(image="i", command=("true",), bin_dir="../x")), "bin_dir"),
        (PodContribution(tools=ToolsSpec(image="i", command=("true",), bin_dir="/abs")), "bin_dir"),
        (PodContribution(init_containers=[_side("demo-tools")], tools=_tools()), "duplicado"),
        (
            PodContribution(main_env=[{"name": "PATH", "value": "/x"}], tools=_tools()),
            "PATH",
        ),
    ],
)
def test_bad_tools_are_rejected(settings, contrib, fragment):
    with pytest.raises(ContributionError, match=fragment):
        _build(settings, ("demo", contrib))


# --- skills entregues pela imagem de tools (ToolsSpec.skills) ----------------


def _image_skills(*names, **kw):
    return PodContribution(tools=ToolsSpec(image="example/tools:1", command=("true",), skills=names, **kw))


def test_image_skills_are_subpath_mounts_of_the_tools_volume(settings):
    pod = _build(settings, ("demo", _image_skills("demo", "demo-extra", skills_dir="agent/skills")))
    spec = pod["spec"]
    mounts = {m["mountPath"]: m for m in spec["containers"][0]["volumeMounts"]}
    assert mounts["/home/kirocrew/.kiro/skills/demo"] == {
        "name": "demo-tools",
        "mountPath": "/home/kirocrew/.kiro/skills/demo",
        "subPath": "agent/skills/demo",
        "readOnly": True,
    }
    assert mounts["/home/kirocrew/.kiro/skills/demo-extra"]["subPath"] == "agent/skills/demo-extra"
    # sem ConfigMap: o conteúdo vem da imagem
    assert all("configMap" not in v for v in spec["volumes"])
    assert collect_files([("demo", _image_skills("demo"))]) == {}


def test_image_skills_follow_the_image_tag_in_the_spec_hash(settings):
    ann = tpl.SPEC_HASH_ANNOTATION

    def h(image, *skills):
        t = ToolsSpec(image=image, command=("true",), skills=skills)
        return _build(settings, ("demo", PodContribution(tools=t)))["metadata"]["annotations"][ann]

    assert h("example/tools:1", "demo") != h("example/tools:2", "demo")
    assert h("example/tools:1", "demo") != h("example/tools:1")


@pytest.mark.parametrize(
    "contrib, fragment",
    [
        (_image_skills("other"), "skill 'other'"),
        (_image_skills("Demo"), "skill 'Demo'"),
        (_image_skills("demo", "demo"), "repetidos"),
        (_image_skills("demo", skills_dir="../x"), "skills_dir"),
        (_image_skills("demo", skills_dir="/abs"), "skills_dir"),
    ],
)
def test_bad_image_skills_are_rejected(settings, contrib, fragment):
    with pytest.raises(ContributionError, match=fragment):
        _build(settings, ("demo", contrib))


def test_image_skill_and_configmap_skill_with_the_same_name_clash(settings):
    both = PodContribution(
        tools=ToolsSpec(image="i", command=("true",), skills=("demo",)),
        skills={"demo": SKILL},
    )
    with pytest.raises(ContributionError, match="mountPath"):
        _build(settings, ("demo", both))


def test_image_skill_and_configmap_skill_can_coexist_under_different_names(settings):
    both = PodContribution(
        tools=ToolsSpec(image="i", command=("true",), skills=("demo",)),
        skills={"demo-small": SKILL.replace("name: demo", "name: demo-small")},
    )
    pod = _build(settings, ("demo", both))
    paths = {m["mountPath"] for m in pod["spec"]["containers"][0]["volumeMounts"]}
    assert {"/home/kirocrew/.kiro/skills/demo", "/home/kirocrew/.kiro/skills/demo-small"} <= paths


# --- skills: SKILL.md descoberto pelo Kiro Crew -------------------------------

SKILL = "---\nname: demo\ndescription: Demo skill\n---\n\n# Demo\n"


def test_skill_is_mounted_read_only_under_kiro_skills(settings):
    contrib = PodContribution(skills={"demo": SKILL})
    pod = _build(settings, ("demo", contrib))
    vol = next(v for v in pod["spec"]["volumes"] if v["name"] == "demo-skill")
    assert vol["configMap"] == {
        "name": "krewhub-ext-files-alice",
        "items": [{"key": "demo.skills.demo.md", "path": "SKILL.md"}],
    }
    main = pod["spec"]["containers"][0]
    assert {
        "name": "demo-skill",
        "mountPath": "/home/kirocrew/.kiro/skills/demo",
        "readOnly": True,
    } in main["volumeMounts"]
    assert collect_files([("demo", contrib)]) == {"demo.skills.demo.md": SKILL}


def test_skill_content_changes_the_spec_hash(settings):
    ann = tpl.SPEC_HASH_ANNOTATION

    def h(body):
        c = PodContribution(skills={"demo": SKILL + body})
        return _build(settings, ("demo", c))["metadata"]["annotations"][ann]

    assert h("a") != h("b")


def test_skill_and_files_share_the_configmap_without_clashing(settings):
    contrib = PodContribution(files={"cfg.yaml": "x"}, skills={"demo-aws": SKILL.replace("name: demo", "name: demo-aws")})
    data = collect_files([("demo", contrib)])
    assert data == {
        "demo.cfg.yaml": "x",
        "demo.skills.demo-aws.md": SKILL.replace("name: demo", "name: demo-aws"),
    }
    pod = _build(settings, ("demo", contrib))
    names = {v["name"] for v in pod["spec"]["volumes"]}
    assert {"demo-files", "demo-aws-skill"} <= names


@pytest.mark.parametrize(
    "skills, fragment",
    [
        ({"other": "---\nname: other\n---\n"}, "precisa se chamar"),
        ({"Demo": SKILL}, "precisa se chamar"),
        ({"demo": "# sem frontmatter\n"}, "frontmatter"),
        ({"demo": "---\nname: outro\n---\n"}, "frontmatter"),
        ({"demo": "---\ndescription: x\n---\nname: demo\n"}, "frontmatter"),
        ({"demo": "---\nname: demo\n"}, "frontmatter"),
    ],
)
def test_bad_skills_are_rejected(settings, skills, fragment):
    with pytest.raises(ContributionError, match=fragment):
        _build(settings, ("demo", PodContribution(skills=skills)))


def test_skill_file_name_collision_is_rejected(settings):
    contrib = PodContribution(files={"skills.demo.md": "x"}, skills={"demo": SKILL})
    with pytest.raises(ContributionError, match="colide"):
        _build(settings, ("demo", contrib))
