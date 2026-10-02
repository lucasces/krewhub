"""Merge de `PodContribution` no spec do Pod do dev, com validação de
conflitos. Roda ANTES do overlay JSON Patch e do hash do spec (ver
`k8s_templates.build_pod`): o overlay do admin sempre tem a última
palavra, e mudar a contribuição de uma extensão muda o hash e recria o
Pod.

Regras (cada violação levanta `ContributionError`):
- containers/initContainers/volumes da extensão `<id>` se chamam `<id>`
  ou começam com `<id>-` -- extensões não colidem entre si nem com o core;
- nunca o container `kirocrew`, nem os volumes `home`/`tmp`;
- sidecar sem `readinessProbe` (a readiness do Pod é global: um sidecar
  não pronto tiraria o Pod do Service e do CHP);
- sem `privileged`, sem volume `hostPath`;
- sidecars e mounts extras só enxergam volumes da própria extensão
  (nunca o `home` com o workspace do dev);
- variáveis de ambiente no `kirocrew` não podem repetir nome já existente
  nem de outra extensão; mountPath idem;
- `tools` e `skills` são expandidos aqui em volumes/initContainer/mounts/PATH
  comuns e passam pelas mesmas regras acima."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.extensions.base import (
    SKILLS_ROOT,
    TOOLS_POPULATE_DIR,
    PodContribution,
    ToolsSpec,
    files_volume_name,
    tools_mount_path,
)

MAIN_CONTAINER = "kirocrew"
CORE_VOLUMES = ("home", "tmp")
FILES_CONFIGMAP_PREFIX = "krewhub-ext-files-"
#: `PATH` padrão da imagem do kirocrew (Debian). Variável de ambiente do Pod
#: substitui a da imagem por inteiro (o k8s não expande `$(PATH)` com ENV de
#: imagem), então o PATH com as ferramentas das extensões é montado em cima disto.
DEFAULT_MAIN_PATH = "/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


class ContributionError(ValueError):
    pass


@dataclass
class ExtPlan:
    """O que o reconcile precisa saber de uma extensão ativa: sua
    contribuição ao Pod e as chaves (`<id>.<campo>`) de campos `generated`
    que o Secret `krewhub-ext-<slug>` deve conter."""

    ext_id: str
    contribution: PodContribution
    generated_keys: tuple[str, ...] = field(default_factory=tuple)


def files_configmap_name(slug: str) -> str:
    return f"{FILES_CONFIGMAP_PREFIX}{slug}"


def files_key(ext_id: str, filename: str) -> str:
    return f"{ext_id}.{filename}"


def skill_key(ext_id: str, skill: str) -> str:
    return f"{ext_id}.skills.{skill}.md"


def _tools_parts(ext_id: str, tools: ToolsSpec) -> tuple[dict, dict, dict, str]:
    """(volume, initContainer, mount do kirocrew, diretório pro PATH)."""
    name = f"{ext_id}-tools"
    if not tools.command:
        raise ContributionError(f"extensão {ext_id!r}: tools.command vazio")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*", tools.bin_dir):
        raise ContributionError(f"extensão {ext_id!r}: tools.bin_dir inválido: {tools.bin_dir!r}")
    volume = {"name": name, "emptyDir": {"sizeLimit": tools.size_limit}}
    init = {
        "name": name,
        "image": tools.image,
        "imagePullPolicy": "IfNotPresent",
        "command": list(tools.command),
        "volumeMounts": [{"name": name, "mountPath": TOOLS_POPULATE_DIR}],
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"memory": "256Mi"},
        },
    }
    mount_path = tools_mount_path(ext_id)
    mount = {"name": name, "mountPath": mount_path, "readOnly": True}
    return volume, init, mount, f"{mount_path}/{tools.bin_dir}"


def _frontmatter_names(content: str) -> list[str]:
    """Valores de `name:` no frontmatter (entre os dois `---` iniciais)."""
    lines = content.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return []
    names: list[str] = []
    for line in lines[1:]:
        if line.rstrip() == "---":
            return names
        if line.startswith("name:"):
            names.append(line[len("name:"):].strip())
    return []


def _skill_parts(ext_id: str, skill: str, content: str, slug: str) -> tuple[dict, dict]:
    """(volume, mount do kirocrew) de uma skill; o volume é um ConfigMap só
    com o `SKILL.md`, montado como diretório em `~/.kiro/skills/<nome>`."""
    if not _SKILL_NAME_RE.match(skill) or not _owns(ext_id, skill):
        raise ContributionError(
            f"extensão {ext_id!r}: skill {skill!r} precisa se chamar {ext_id!r} ou começar com "
            f"'{ext_id}-' ([a-z0-9-])"
        )
    if skill not in _frontmatter_names(content):
        raise ContributionError(
            f"extensão {ext_id!r}: skill {skill!r} precisa começar com frontmatter contendo "
            f"'name: {skill}'"
        )
    name = f"{skill}-skill"
    volume = {
        "name": name,
        "configMap": {
            "name": files_configmap_name(slug),
            "items": [{"key": skill_key(ext_id, skill), "path": "SKILL.md"}],
        },
    }
    mount = {"name": name, "mountPath": f"{SKILLS_ROOT}/{skill}", "readOnly": True}
    return volume, mount


def _owns(ext_id: str, name: str) -> bool:
    return name == ext_id or name.startswith(f"{ext_id}-")


def _check_name(ext_id: str, kind: str, name: Any) -> None:
    if not isinstance(name, str) or not _owns(ext_id, name):
        raise ContributionError(
            f"extensão {ext_id!r}: {kind} {name!r} precisa se chamar {ext_id!r} ou começar com '{ext_id}-'"
        )


def _check_container(ext_id: str, c: dict, own_volumes: set[str]) -> None:
    name = c.get("name")
    _check_name(ext_id, "container", name)
    if name == MAIN_CONTAINER:
        raise ContributionError(f"extensão {ext_id!r}: container {MAIN_CONTAINER!r} é reservado")
    if "readinessProbe" in c:
        raise ContributionError(
            f"extensão {ext_id!r}: container {name!r} não pode ter readinessProbe "
            "(a readiness do Pod é global)"
        )
    if (c.get("securityContext") or {}).get("privileged"):
        raise ContributionError(f"extensão {ext_id!r}: container {name!r} não pode ser privileged")
    for m in c.get("volumeMounts") or []:
        if m.get("name") not in own_volumes:
            raise ContributionError(
                f"extensão {ext_id!r}: container {name!r} monta volume {m.get('name')!r} "
                "que não é da extensão"
            )


def _check_volume(ext_id: str, v: dict) -> None:
    _check_name(ext_id, "volume", v.get("name"))
    if v["name"] in CORE_VOLUMES:
        raise ContributionError(f"extensão {ext_id!r}: volume {v['name']!r} é reservado")
    if "hostPath" in v:
        raise ContributionError(f"extensão {ext_id!r}: volume hostPath não é permitido")


def merge_contributions(
    spec: dict, slug: str, contributions: Iterable[tuple[str, PodContribution]]
) -> dict[str, str]:
    """Aplica as contribuições em `spec` (in-place) e devolve as
    anotações extras pro metadata do Pod. `contributions` é
    `[(ext_id, PodContribution), ...]` na ordem de habilitação."""
    main = spec["containers"][0]
    if main["name"] != MAIN_CONTAINER:
        raise ContributionError("containers[0] precisa ser o kirocrew")

    seen_containers = {c["name"] for c in spec["containers"]} | {
        c["name"] for c in spec.get("initContainers", [])
    }
    seen_volumes = {v["name"] for v in spec.get("volumes", [])}
    seen_env = {e["name"] for e in main.get("env", [])}
    seen_mounts = {m["mountPath"] for m in main.get("volumeMounts", [])}
    annotations: dict[str, str] = {}
    tool_dirs: list[str] = []

    for ext_id, contrib in contributions:
        volumes = list(contrib.volumes)
        init_containers = list(contrib.init_containers)
        main_mounts = list(contrib.main_volume_mounts)
        if contrib.files:
            volumes.append(_files_volume(ext_id, slug, contrib.files))
        if contrib.tools:
            volume, init, mount, bin_path = _tools_parts(ext_id, contrib.tools)
            volumes.append(volume)
            init_containers.append(init)
            main_mounts.append(mount)
            tool_dirs.append(bin_path)
        for skill, content in sorted(contrib.skills.items()):
            volume, mount = _skill_parts(ext_id, skill, content, slug)
            if any(files_key(ext_id, f) == skill_key(ext_id, skill) for f in contrib.files):
                raise ContributionError(f"extensão {ext_id!r}: arquivo colide com a skill {skill!r}")
            volumes.append(volume)
            main_mounts.append(mount)
        for v in volumes:
            _check_volume(ext_id, v)
            if v["name"] in seen_volumes:
                raise ContributionError(f"extensão {ext_id!r}: volume {v['name']!r} duplicado")
            seen_volumes.add(v["name"])
        own_volumes = {v["name"] for v in volumes}

        for kind, items in (("containers", contrib.containers), ("initContainers", init_containers)):
            for c in items:
                _check_container(ext_id, c, own_volumes)
                if c["name"] in seen_containers:
                    raise ContributionError(f"extensão {ext_id!r}: container {c['name']!r} duplicado")
                seen_containers.add(c["name"])
                spec.setdefault(kind, []).append(c)

        for e in contrib.main_env:
            if e.get("name") in seen_env:
                raise ContributionError(
                    f"extensão {ext_id!r}: variável {e.get('name')!r} já definida no kirocrew"
                )
            seen_env.add(e["name"])
            main.setdefault("env", []).append(e)

        for m in main_mounts:
            if m.get("name") not in own_volumes:
                raise ContributionError(
                    f"extensão {ext_id!r}: mount no kirocrew de volume {m.get('name')!r} "
                    "que não é da extensão"
                )
            if m["mountPath"] in seen_mounts:
                raise ContributionError(
                    f"extensão {ext_id!r}: mountPath {m['mountPath']!r} já usado no kirocrew"
                )
            seen_mounts.add(m["mountPath"])
            main.setdefault("volumeMounts", []).append(m)

        spec.setdefault("volumes", []).extend(volumes)
        annotations.update(contrib.annotations)

    if tool_dirs:
        if "PATH" in seen_env:
            raise ContributionError("variável 'PATH' já definida no kirocrew; ferramentas de extensão a estendem")
        main.setdefault("env", []).append(
            {"name": "PATH", "value": ":".join([*tool_dirs, DEFAULT_MAIN_PATH])}
        )

    return annotations


def _files_volume(ext_id: str, slug: str, files: dict[str, str]) -> dict:
    return {
        "name": files_volume_name(ext_id),
        "configMap": {
            "name": files_configmap_name(slug),
            "items": [
                {"key": files_key(ext_id, fname), "path": fname} for fname in sorted(files)
            ],
        },
    }


def collect_files(contributions: Sequence[tuple[str, PodContribution]]) -> dict[str, str]:
    """Dados do ConfigMap `krewhub-ext-files-<slug>`: `<id>.<arquivo>` -> conteúdo."""
    data: dict[str, str] = {}
    for ext_id, contrib in contributions:
        for fname, content in contrib.files.items():
            data[files_key(ext_id, fname)] = content
        for skill, content in contrib.skills.items():
            data[skill_key(ext_id, skill)] = content
    return data
