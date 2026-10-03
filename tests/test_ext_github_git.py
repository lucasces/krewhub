"""O `/etc/gitconfig` que a extensão GitHub gera, com o git REAL da imagem do
`kirocrew`: um servidor HTTPS falso (git smart-HTTP via `git http-backend`) só
aceita Basic `x-access-token:<token atual>`, e o token vive só num arquivo que o
teste troca no meio do caminho (como o kubelet faz ao atualizar o Secret).
Pulado se não houver podman, a imagem local ou um openssl pra gerar o
certificado."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from krewhub_ext_github import TOKEN_DIR, render_gitconfig

IMAGE = "ghcr.io/kirodotdev/kirocrew:0.6.0"
OPENSSL_IMAGE = "ghcr.io/lucasces/krewhub-ext-aws-sso:0.2.0-rc.6"
PORT = 8443


def _image_exists(image: str) -> bool:
    if shutil.which("podman") is None:
        return False
    return subprocess.run(["podman", "image", "exists", image], capture_output=True).returncode == 0


def _make_cert(tmp_path: Path) -> bool:
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost", "-keyout", "/out/key.pem", "-out", "/out/cert.pem",
    ]
    if shutil.which("openssl"):
        cmd = [c.replace("/out/", f"{tmp_path}/") for c in cmd]
    elif _image_exists(OPENSSL_IMAGE):
        cmd = ["podman", "run", "--rm", "--pull=never", "--entrypoint", cmd[0], "-v", f"{tmp_path}:/out:z", OPENSSL_IMAGE, *cmd[1:]]
    else:
        return False
    return subprocess.run(cmd, capture_output=True).returncode == 0 and (tmp_path / "cert.pem").exists()


pytestmark = pytest.mark.skipif(not _image_exists(IMAGE), reason=f"podman ou a imagem {IMAGE} não estão disponíveis")

# Servidor: autentica e repassa pro `git http-backend`; registra cada Authorization visto.
SERVER = r'''
import base64, http.server, json, os, ssl, subprocess

ROOT = "/tmp/srv"

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def log_message(self, *a): pass

    def handle_any(self):
        auth = self.headers.get("Authorization", "")
        with open("/tmp/auth.log", "a") as f:
            f.write(json.dumps(auth) + "\n")
        want = "Basic " + base64.b64encode(("x-access-token:" + open("/tmp/expected").read().strip()).encode()).decode()
        if auth != want:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        env = {"PATH": os.environ["PATH"], "GIT_PROJECT_ROOT": ROOT, "GIT_HTTP_EXPORT_ALL": "1",
               "REQUEST_METHOD": self.command, "PATH_INFO": path, "QUERY_STRING": query,
               "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
               "REMOTE_USER": "x-access-token", "REMOTE_ADDR": "127.0.0.1",
               "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", "")}
        out = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True).stdout
        head, _, payload = out.partition(b"\r\n\r\n")
        status = 200
        headers = []
        for line in head.decode().split("\r\n"):
            k, _, v = line.partition(": ")
            if k.lower() == "status":
                status = int(v.split()[0])
            elif k:
                headers.append((k, v))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = handle_any

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8443), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain("/certs/cert.pem", "/certs/key.pem")
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
srv.serve_forever()
'''

# Cliente: cada passo roda git de verdade; o "kubelet" troca o arquivo do token entre eles.
SCRIPT = r'''
set -u
export GIT_TERMINAL_PROMPT=0 GIT_SSL_NO_VERIFY=1 HOME=/tmp/home GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
unset GITHUB_TOKEN
mkdir -p $HOME /tmp/srv
git init -q -b main --bare /tmp/srv/repo.git && git -C /tmp/srv/repo.git config http.receivepack true
git init -q -b main /tmp/seed && git -C /tmp/seed commit -q --allow-empty -m seed && git -C /tmp/seed push -q /tmp/srv/repo.git HEAD:refs/heads/main
python3 /server.py &
sleep 1
URL=https://localhost:8443/repo.git
settoken() { printf %s "$1" > /etc/krewhub/github/token; printf %s "$1" > /tmp/expected; }
r() { echo "$1=$2"; }

settoken tok-one
git clone -q $URL /tmp/w1 2>/dev/null; r clone $?
git -C /tmp/w1 commit -q --allow-empty -m change && git -C /tmp/w1 push -q origin HEAD:refs/heads/main 2>/dev/null; r push $?

settoken tok-two
git -C /tmp/w1 fetch -q origin 2>/dev/null; r fetch_after_rotation $?

printf %s tok-one > /etc/krewhub/github/token
git -C /tmp/w1 fetch -q origin 2>/dev/null; r fetch_with_wrong_token $?

: > /etc/krewhub/github/token
git clone -q $URL /tmp/w2 2>/dev/null; r clone_without_token $?
echo "gitconfig_has_token=$(grep -c tok- /etc/gitconfig)"
'''


def test_git_clone_push_and_rotation_use_the_token_file_through_the_helper(tmp_path):
    if not _make_cert(tmp_path):
        pytest.skip("sem openssl pra gerar o certificado de teste")
    (tmp_path / "gitconfig").write_text(render_gitconfig("localhost:8443"))
    (tmp_path / "server.py").write_text(SERVER)
    (tmp_path / "script.sh").write_text(SCRIPT)
    for f in tmp_path.iterdir():
        f.chmod(0o644)
    result = subprocess.run(
        [
            "podman", "run", "--rm", "--pull=never", "--read-only",
            "--tmpfs", "/tmp:rw,mode=1777", "--tmpfs", f"{TOKEN_DIR}:rw,mode=0777",
            "-v", f"{tmp_path / 'gitconfig'}:/etc/gitconfig:ro,z",
            "-v", f"{tmp_path / 'server.py'}:/server.py:ro,z",
            "-v", f"{tmp_path}:/certs:ro,z",
            "-v", f"{tmp_path / 'script.sh'}:/script.sh:ro,z",
            "--entrypoint", "bash", IMAGE, "/script.sh",
        ],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr
    out = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert out == {
        "clone": "0",
        "push": "0",
        "fetch_after_rotation": "0",  # o helper leu o arquivo novo, sem recriar nada
        "fetch_with_wrong_token": "128",  # o servidor recusa: a senha enviada é mesmo a do arquivo
        "clone_without_token": "128",  # sem token o git falha sem prompt
        "gitconfig_has_token": "0",
    }
