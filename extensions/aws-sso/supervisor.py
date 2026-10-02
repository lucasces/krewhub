#!/usr/bin/env python3
"""Supervisor do sidecar `aws-sso` (somente stdlib).

Sobe o `aws-sso ecs server`, mantém as credenciais carregadas e publica o
estado em `/state/status.json`. É o ÚNICO escritor desse arquivo; o
KrewHub (via `kubectl exec`-equivalente) só deixa pedidos em `/state/req/`:

- `req/profile`  -- perfil a assumir (`aws-sso list` -> coluna Profile,
  sempre `<id da conta, 12 dígitos>:<papel>` por causa do `ProfileFormat`
  gerado pela extensão)
- `req/refresh`  -- recarregar a lista de contas/papéis
- `req/reload`   -- recarregar as credenciais do papel atual

`/state/login_ok` é criado pelo fluxo de login (`aws-sso login && touch`)."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("AWS_SSO_STATE", "/state"))
CONFIG_SRC = Path(os.environ.get("AWS_SSO_CONFIG_SRC", "/etc/aws-sso/config.yaml"))
AWS_SSO = os.environ.get("AWS_SSO_BIN", "aws-sso")
PORT = os.environ.get("AWS_SSO_PORT", "4144")
POLL_SECONDS = float(os.environ.get("AWS_SSO_POLL", "2"))
RELOAD_SECONDS = float(os.environ.get("AWS_SSO_RELOAD", "1200"))
CMD_TIMEOUT = 60

PROFILE_RE = re.compile(r"^\d{12}:[A-Za-z0-9_+=,.@-]{1,64}$")


def log(msg: str) -> None:
    print(f"[supervisor] {msg}", file=sys.stderr, flush=True)


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [AWS_SSO, *args], capture_output=True, text=True, timeout=CMD_TIMEOUT, check=False
    )


def take(path: Path) -> str | None:
    """Consome um arquivo de pedido (lê e apaga). `None` se não existe."""
    try:
        value = path.read_text().strip()
        path.unlink()
        return value
    except FileNotFoundError:
        return None


class Supervisor:
    def __init__(self) -> None:
        self.server: subprocess.Popen | None = None
        self.profile = ""
        self.loaded = False
        self.last_load = 0.0
        self.roles: list[str] = []
        self.labels: dict[str, str] = {}
        self.roles_fetched = False
        self.error = ""
        self.next_server_try = 0.0

    # --- preparação -------------------------------------------------------

    def setup(self) -> None:
        cfg_dir = Path(os.environ["HOME"]) / ".config" / "aws-sso"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (STATE / "req").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CONFIG_SRC, cfg_dir / "config.yaml")

    def ensure_server(self) -> bool:
        if self.server is not None and self.server.poll() is None:
            return True
        if time.time() < self.next_server_try:
            return False
        self.next_server_try = time.time() + 5
        token = os.environ.get("KREWHUB_AWS_SSO_TOKEN", "")
        if token:
            run(["setup", "ecs", "auth", "--bearer-token", token])
        self.server = subprocess.Popen(
            [AWS_SSO, "ecs", "server", "--bind-ip", "127.0.0.1", "--port", PORT],
            stdout=subprocess.DEVNULL,
        )
        time.sleep(0.3)
        return self.server.poll() is None

    # --- operações --------------------------------------------------------

    def refresh_roles(self) -> None:
        run(["cache"])
        res = run(["list", "--csv", "Profile", "AccountName"])
        if res.returncode != 0:
            log(f"list falhou: rc={res.returncode}")
            (STATE / "login_ok").unlink(missing_ok=True)
            self.roles, self.labels, self.roles_fetched, self.loaded = [], {}, False, False
            self.error = "Sessão SSO expirada ou inválida; faça login de novo."
            return
        rows = [r for r in csv.reader(io.StringIO(res.stdout)) if r and r[0] != "Profile"]
        # o nome da conta é só rótulo de exibição: nunca vira argumento de comando
        self.labels = {r[0]: (r[1] if len(r) > 1 else "") for r in rows}
        self.roles = [r[0] for r in rows]
        self.roles_fetched = True
        self.error = ""

    def load_creds(self, *, refresh: bool) -> None:
        args = ["ecs", "load", "--profile", self.profile, "--server", f"localhost:{PORT}"]
        if refresh:
            args.append("--sts-refresh")
        res = run(args)
        if res.returncode == 0:
            self.loaded, self.last_load, self.error = True, time.time(), ""
        else:
            log(f"ecs load falhou: rc={res.returncode}")
            self.loaded = False
            self.error = "Não foi possível carregar as credenciais deste papel."

    # --- ciclo ------------------------------------------------------------

    def tick(self) -> None:
        server_up = self.ensure_server()
        logged_in = (STATE / "login_ok").exists()

        if not logged_in:
            self.roles, self.labels, self.roles_fetched, self.loaded = [], {}, False, False
            take(STATE / "req" / "refresh")
            take(STATE / "req" / "reload")
            take(STATE / "req" / "profile")
        else:
            if take(STATE / "req" / "refresh") is not None or not self.roles_fetched:
                self.refresh_roles()
                logged_in = (STATE / "login_ok").exists()
            wanted = take(STATE / "req" / "profile")
            if wanted is not None:
                if PROFILE_RE.match(wanted) and (not self.roles or wanted in self.roles):
                    self.profile = wanted
                    self.load_creds(refresh=False)
                else:
                    self.error = "Perfil desconhecido."
            elif not self.profile and len(self.roles) == 1:
                self.profile = self.roles[0]
                self.load_creds(refresh=False)
            if self.profile and server_up and logged_in:
                due = time.time() - self.last_load > RELOAD_SECONDS
                if take(STATE / "req" / "reload") is not None or (self.loaded and due):
                    self.load_creds(refresh=True)

        self.write_status(server_up, logged_in)

    def write_status(self, server_up: bool, logged_in: bool) -> None:
        data = {
            "ts": time.time(),
            "server": server_up,
            "logged_in": logged_in,
            "profile": self.profile,
            "loaded": self.loaded and server_up,
            "roles": len(self.roles),
            "role_names": self.roles[:50],
            "role_labels": {p: self.labels.get(p, "") for p in self.roles[:50]},
            "profile_label": self.labels.get(self.profile, ""),
            "error": self.error,
        }
        tmp = STATE / "status.json.tmp"
        tmp.write_text(json.dumps(data))
        tmp.replace(STATE / "status.json")


def main() -> None:
    sup = Supervisor()
    sup.setup()
    while True:
        try:
            sup.tick()
        except Exception as exc:  # o supervisor nunca morre: o Pod ficaria sem credenciais
            log(f"tick falhou: {type(exc).__name__}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
