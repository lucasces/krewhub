"""`tools_copy_command` num cenário igual ao do cluster.

A raiz de um emptyDir nasce do root (modo 0777) e o initContainer roda sem
root e sem capabilities: ele não é dono da raiz e não tem `CAP_FOWNER`, então
tudo que tenta acertar data/permissão dela (`cp -a`, `--preserve`) dá EPERM.
Aqui a cópia roda de verdade num container com esse mesmo endurecimento
(pulado se não houver podman ou a imagem base local)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.extensions.base import TOOLS_POPULATE_DIR, tools_copy_command

IMAGE = "docker.io/library/debian:bookworm-slim"


def _podman_ready() -> bool:
    if shutil.which("podman") is None:
        return False
    return subprocess.run(["podman", "image", "exists", IMAGE], capture_output=True).returncode == 0


needs_podman = pytest.mark.skipif(not _podman_ready(), reason=f"podman ou a imagem {IMAGE} não estão disponíveis")


def test_command_copies_without_preserving_attributes_of_the_destination_root():
    sh, flag, script = tools_copy_command("/opt/x")
    assert (sh, flag) == ("sh", "-c")
    assert script == f"cp -dR /opt/x/. {TOOLS_POPULATE_DIR}/"


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "bin").mkdir(parents=True)
    (src / "app").mkdir()
    tool = src / "app" / "tool"
    tool.write_text("#!/bin/sh\necho ok\n")
    tool.chmod(0o755)
    (src / "bin" / "tool").symlink_to("../app/tool")
    for p in [src, *src.rglob("*")]:
        if not p.is_symlink():
            p.chmod(p.stat().st_mode | 0o055)
    return src


def _run_hardened(src: Path, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "podman", "run", "--rm", "--pull=never",
            "--user", "1000:1000", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            # igual ao emptyDir: raiz do root, gravável por todos
            "--tmpfs", f"{TOOLS_POPULATE_DIR}:rw,mode=0777",
            "-v", f"{src}:/opt/x:ro",
            IMAGE, "sh", "-c", script,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


CHECKS = (
    f"test \"$(stat -c %u {TOOLS_POPULATE_DIR})\" = 0 && "
    f"test -L {TOOLS_POPULATE_DIR}/bin/tool && "
    f"test -x {TOOLS_POPULATE_DIR}/app/tool && "
    f"test \"$({TOOLS_POPULATE_DIR}/bin/tool)\" = ok"
)


@needs_podman
def test_old_cp_a_fails_on_a_root_owned_destination_under_the_hardened_init_container(tmp_path):
    result = _run_hardened(_source(tmp_path), f"cp -a --no-preserve=ownership /opt/x/. {TOOLS_POPULATE_DIR}/")
    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr


@needs_podman
def test_command_populates_a_root_owned_destination_under_the_hardened_init_container(tmp_path):
    script = tools_copy_command("/opt/x")[2]
    result = _run_hardened(_source(tmp_path), f"{script} && {CHECKS}")
    assert result.returncode == 0, result.stderr
