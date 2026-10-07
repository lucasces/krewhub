"""Helper `credential_process` (extensions/aws-sso/credential_process.py) contra
um servidor HTTP falso que imita o `aws-sso ecs server`: slots em
`/slot/<perfil>`, o padrão em `/`, bearer obrigatório. O mesmo helper e o
mesmo arquivo de perfis do supervisor são exercitados com o AWS CLI real
dentro da imagem da extensão (pulado se não houver podman ou a imagem)."""

from __future__ import annotations

import http.server
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "extensions/aws-sso/credential_process.py"
TOKEN = "tok-123"
ROLE_A, ROLE_B = "111111111111:Admin", "222222222222:a+b=c,d.e@f-g"


def _creds(name: str) -> dict:
    return {
        "AccessKeyId": f"AKIA-{name}",
        "SecretAccessKey": f"secret-{name}",
        "Token": f"token-{name}",
        "Expiration": "2099-01-01T00:00:00Z",
    }


class FakeEcsServer(http.server.BaseHTTPRequestHandler):
    slots = {ROLE_B: _creds("B")}
    default = _creds("A")

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(403, {"code": "403", "message": "Invalid authorization token"})
        path = urllib.parse.unquote(self.path)
        if path == "/":
            return self._send(200, self.default)
        if path.startswith("/slot/") and path[6:] in self.slots:
            return self._send(200, self.slots[path[6:]])
        self._send(404, {"code": "404", "message": "Credentials unavailable"})

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def ecs():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeEcsServer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _run(ecs, *args, token=TOKEN):
    env = {"KREWHUB_AWS_SSO_ENDPOINT": ecs, "PATH": "/usr/bin:/bin"}
    if token:
        env["KREWHUB_AWS_SSO_TOKEN"] = token
    return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, env=env, timeout=30)


def test_slot_credentials_come_out_in_the_credential_process_format(ecs):
    out = _run(ecs, ROLE_B)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {
        "Version": 1,
        "AccessKeyId": "AKIA-B",
        "SecretAccessKey": "secret-B",
        "SessionToken": "token-B",
        "Expiration": "2099-01-01T00:00:00Z",
    }


def test_default_flag_reads_the_unslotted_endpoint(ecs):
    out = _run(ecs, ROLE_A, "--default")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["AccessKeyId"] == "AKIA-A"


def test_unloaded_role_fails_with_guidance_for_the_agent(ecs):
    out = _run(ecs, ROLE_A)
    assert out.returncode == 1 and out.stdout == ""
    assert f"credentials for '{ROLE_A}' are not loaded" in out.stderr
    assert "AWS SSO card in KrewHub" in out.stderr


def test_wrong_token_never_echoes_the_secret(ecs):
    out = _run(ecs, ROLE_B, token="nope")
    assert out.returncode == 1 and "HTTP 403" in out.stderr
    assert "nope" not in out.stderr + out.stdout


def test_unreachable_endpoint_is_reported_not_raised():
    out = _run("http://127.0.0.1:9", ROLE_B)
    assert out.returncode == 1 and "unreachable" in out.stderr and "Traceback" not in out.stderr


def test_usage_error_without_a_profile(ecs):
    out = _run(ecs)
    assert out.returncode == 1 and "usage" in out.stderr


# --- AWS CLI real, na imagem da extensão --------------------------------------

IMAGE = "ghcr.io/lucasces/krewhub-ext-aws-sso:0.2.0-rc.5"


def _image_ready() -> bool:
    if shutil.which("podman") is None:
        return False
    return subprocess.run(["podman", "image", "exists", IMAGE], capture_output=True).returncode == 0


def _load_supervisor(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("aws_sso_supervisor_helper", ROOT / "extensions/aws-sso/supervisor.py")
    sup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sup)
    monkeypatch.setattr(sup, "STATE", tmp_path / "state")
    monkeypatch.setattr(sup, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(sup, "HELPER", "/helper/krewhub-aws-sso-creds")
    (tmp_path / "state").mkdir()
    return sup


SCRIPT = """
set -eu
aws configure list-profiles | sort
for p in "$@"; do
  echo "--- $p"
  aws configure export-credentials --profile "$p" --format process | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKeyId"])'
  AWS_PROFILE="$p" aws configure export-credentials --format process | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKeyId"])'
done
"""

needs_podman = pytest.mark.skipif(not _image_ready(), reason=f"podman ou a imagem {IMAGE} não estão disponíveis")


@needs_podman
def test_real_aws_cli_resolves_every_managed_profile_to_its_own_slot(tmp_path, monkeypatch):
    sup = _load_supervisor(monkeypatch, tmp_path)
    s = sup.Supervisor()
    s.set_selected([ROLE_A, ROLE_B])

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeEcsServer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        (tmp_path / "profiles").chmod(0o755)
        for f in (tmp_path / "profiles").iterdir():
            f.chmod(0o644)
        result = subprocess.run(
            [
                "podman", "run", "--rm", "--pull=never", "--network=host",
                "--user", "1000:1000", "--read-only", "--tmpfs", "/tmp",
                "-v", f"{tmp_path / 'profiles'}:/profiles:ro",
                "-v", f"{HELPER}:/helper/krewhub-aws-sso-creds:ro",
                "-e", "AWS_CONFIG_FILE=/profiles/config",
                "-e", f"KREWHUB_AWS_SSO_TOKEN={TOKEN}",
                "-e", f"KREWHUB_AWS_SSO_ENDPOINT=http://127.0.0.1:{server.server_address[1]}",
                "--entrypoint", "sh", IMAGE,
                "-c", f'export PATH="/opt/krewhub-tools/bin:$PATH" HOME=/tmp; {SCRIPT}', "sh", ROLE_A, ROLE_B,
            ],
            capture_output=True, text=True, timeout=180,
        )
    finally:
        server.shutdown()
    assert result.returncode == 0, result.stderr
    # o primeiro papel é o padrão (`--default`, endpoint `/`); o outro vem do slot nomeado
    assert result.stdout.splitlines() == [
        ROLE_A, ROLE_B,
        f"--- {ROLE_A}", "AKIA-A", "AKIA-A",
        f"--- {ROLE_B}", "AKIA-B", "AKIA-B",
    ]


# --- servidor ECS real do aws-sso, na imagem da extensão ----------------------

REAL_SERVER_SCRIPT = r'''
import importlib.util, json, os, subprocess, sys, time, urllib.request
os.environ.update(HOME="/tmp/h", KREWHUB_AWS_SSO_TOKEN="s3cret", AWS_SSO_PORT="4144")
os.makedirs("/tmp/h/.config/aws-sso")
open("/tmp/h/.config/aws-sso/config.yaml", "w").write(open("/cfg/config.yaml").read())
subprocess.run(["aws-sso", "setup", "ecs", "auth", "--bearer-token", "s3cret"], capture_output=True, check=True)
server = subprocess.Popen(["aws-sso", "ecs", "server", "--bind-ip", "127.0.0.1", "--port", "4144"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2)

def put(path, name, role):
    creds = {"accessKeyId": "AKIA-" + role, "secretAccessKey": "s", "sessionToken": "t",
             "expiration": 4102444800000, "roleName": role, "accountId": int(name[:12])}
    req = urllib.request.Request("http://127.0.0.1:4144" + path, method="PUT",
                                 data=json.dumps({"ProfileName": name, "Creds": creds}).encode())
    req.add_header("Authorization", "Bearer s3cret")
    req.add_header("Content-Type", "application/json")
    urllib.request.urlopen(req, timeout=5).close()

def helper(*args):
    out = subprocess.run([sys.executable, "/helper.py", *args], capture_output=True, text=True,
                         env={**os.environ, "KREWHUB_AWS_SSO_ENDPOINT": "http://127.0.0.1:4144"})
    return out.returncode, (json.loads(out.stdout)["AccessKeyId"] if out.returncode == 0 else out.stderr.strip())

A, B = "111111111111:Admin", "222222222222:a+b=c,d.e@f-g"
put("/", A, "Admin")
put("/slot/" + urllib.parse.quote(B, safe=""), B, "Dev")
print(helper(A, "--default"), helper(B))

spec = importlib.util.spec_from_file_location("sup", "/sup.py")
sup = importlib.util.module_from_spec(spec); spec.loader.exec_module(sup)
sup.delete_slot(B)
sup.delete_slot(B)  # idempotente: 404 é tolerado
print(helper(B))
print(helper(A, "--default"))
server.kill()
'''


@needs_podman
def test_helper_and_supervisor_slot_removal_work_against_the_real_ecs_server(tmp_path):
    from krewhub_ext_aws_sso import render_config

    (tmp_path / "config.yaml").write_text(
        render_config({"start_url": "https://d-1234567890.awsapps.com/start", "sso_region": "us-east-1"})
    )
    (tmp_path / "script.py").write_text("import urllib.parse\n" + REAL_SERVER_SCRIPT)
    result = subprocess.run(
        [
            "podman", "run", "--rm", "--pull=never", "--entrypoint", "python3",
            "-v", f"{tmp_path / 'config.yaml'}:/cfg/config.yaml:ro",
            "-v", f"{tmp_path / 'script.py'}:/script.py:ro",
            "-v", f"{HELPER}:/helper.py:ro",
            "-v", f"{ROOT / 'extensions/aws-sso/supervisor.py'}:/sup.py:ro",
            IMAGE, "/script.py",
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("(")]
    assert lines[0] == "(0, 'AKIA-Admin') (0, 'AKIA-Dev')"
    assert lines[1].startswith("(1, \"krewhub-aws-sso-creds: credentials for '222222222222:a+b=c,d.e@f-g' are not loaded")
    assert lines[2] == "(0, 'AKIA-Admin')"  # o padrão não é apagado
