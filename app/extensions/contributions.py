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
  nem de outra extensão; mountPath idem."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.extensions.base import PodContribution, files_volume_name

MAIN_CONTAINER = "kirocrew"
CORE_VOLUMES = ("home", "tmp")
FILES_CONFIGMAP_PREFIX = "krewhub-ext-files-"


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

    for ext_id, contrib in contributions:
        volumes = list(contrib.volumes)
        if contrib.files:
            volumes.append(_files_volume(ext_id, slug, contrib.files))
        for v in volumes:
            _check_volume(ext_id, v)
            if v["name"] in seen_volumes:
                raise ContributionError(f"extensão {ext_id!r}: volume {v['name']!r} duplicado")
            seen_volumes.add(v["name"])
        own_volumes = {v["name"] for v in volumes}

        for kind, items in (("containers", contrib.containers), ("initContainers", contrib.init_containers)):
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

        for m in contrib.main_volume_mounts:
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
    return data
