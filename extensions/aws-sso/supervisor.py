#!/usr/bin/env python3
"""Supervisor do sidecar `aws-sso` (somente stdlib).

Sobe o `aws-sso ecs server`, mantém as credenciais carregadas e publica o
estado em `/state/status.json`. É o ÚNICO escritor desse arquivo; o
KrewHub (via `kubectl exec`-equivalente) só deixa pedidos em `/state/req/`:

- `req/profiles` -- CONJUNTO desejado de papéis ativos, um por linha
  (`aws-sso list` -> coluna Profile, sempre `<id da conta, 12 dígitos>:<papel>`
  por causa do `ProfileFormat` gerado pela extensão). Substitui o conjunto
  anterior; o primeiro é o padrão (endpoint `/`), os demais são slots
  nomeados (`/slot/<perfil>`).
- `req/refresh`  -- recarregar a lista de contas/papéis
- `req/reload`   -- recarregar as credenciais de todos os papéis ativos

`/state/login_ok` é criado pelo fluxo de login (`aws-sso login && touch`).
`/state/selected` guarda o conjunto ativo (sobrevive a reinício do processo).

O arquivo de perfis do AWS CLI (`PROFILES/config`, num emptyDir que o
`kirocrew` monta somente leitura) lista um perfil por papel ativo, com
`credential_process` apontando pro helper `krewhub-aws-sso-creds`."""

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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATE = Path(os.environ.get("AWS_SSO_STATE", "/state"))
PROFILES_DIR = Path(os.environ.get("AWS_SSO_PROFILES_DIR", "/profiles"))
HELPER = os.environ.get("AWS_SSO_HELPER", "/opt/krewhub-ext/aws-sso/bin/krewhub-aws-sso-creds")
CONFIG_SRC = Path(os.environ.get("AWS_SSO_CONFIG_SRC", "/etc/aws-sso/config.yaml"))
AWS_SSO = os.environ.get("AWS_SSO_BIN", "aws-sso")
PORT = os.environ.get("AWS_SSO_PORT", "4144")
POLL_SECONDS = float(os.environ.get("AWS_SSO_POLL", "2"))
RELOAD_SECONDS = float(os.environ.get("AWS_SSO_RELOAD", "1200"))
RETRY_SECONDS = float(os.environ.get("AWS_SSO_RETRY", "30"))
CMD_TIMEOUT = 60
MAX_ROLES = 10
#: papéis publicados no status (e oferecidos como opções na UI)
MAX_LISTED = 300

PROFILE_RE = re.compile(r"^\d{12}:[A-Za-z0-9_+=,.@-]{1,64}$")


def log(msg: str) -> None:
    print(f"[supervisor] {msg}", file=sys.stderr, flush=True)


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [AWS_SSO, *args], capture_output=True, text=True, timeout=CMD_TIMEOUT, check=False
    )


def delete_slot(profile: str) -> None:
    """`DELETE /slot/<perfil>` no servidor ECS local, com o bearer do KrewHub."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/slot/{urllib.parse.quote(profile, safe='')}", method="DELETE"
    )
    token = os.environ.get("KREWHUB_AWS_SSO_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except urllib.error.HTTPError as exc:
        if exc.code != 404:  # 404 = já não estava carregado
            log(f"remover slot falhou: HTTP {exc.code}")
    except OSError as exc:
        log(f"remover slot falhou: {type(exc).__name__}")


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
        #: papéis ativos, na ordem pedida; o primeiro é o padrão
        self.selected: list[str] = []
        #: papel -> "default" | "slot", conforme carregado no servidor
        self.loaded: dict[str, str] = {}
        self.last_load = 0.0
        self.last_attempt = 0.0
        self.roles: list[str] = []
        self.labels: dict[str, str] = {}
        self.roles_fetched = False
        self.error = ""
        self.next_server_try = 0.0
        self.profiles_written: str | None = None

    # --- preparação -------------------------------------------------------

    def setup(self) -> None:
        cfg_dir = Path(os.environ["HOME"]) / ".config" / "aws-sso"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (STATE / "req").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CONFIG_SRC, cfg_dir / "config.yaml")
        try:
            saved = (STATE / "selected").read_text().split()
        except FileNotFoundError:
            saved = []
        self.selected = [p for p in dict.fromkeys(saved) if PROFILE_RE.match(p)][:MAX_ROLES]
        self.write_profiles()

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
            self.drop_session()
            self.error = "Sessão SSO expirada ou inválida; faça login de novo."
            return
        rows = [r for r in csv.reader(io.StringIO(res.stdout)) if r and r[0] != "Profile"]
        # o nome da conta é só rótulo de exibição: nunca vira argumento de comando
        self.labels = {r[0]: (r[1] if len(r) > 1 else "") for r in rows}
        self.roles = [r[0] for r in rows]
        self.roles_fetched = True
        self.error = ""

    def drop_session(self) -> None:
        """Sem login não há credenciais: a seleção fica, o resto é descartado
        (o servidor ECS perde os slots junto com o token SSO)."""
        self.roles, self.labels, self.roles_fetched, self.loaded = [], {}, False, {}

    def apply_selection(self, wanted: str) -> None:
        profiles = list(dict.fromkeys(wanted.split()))
        if not profiles:
            self.error = "Escolha ao menos um papel."
            return
        if not all(PROFILE_RE.match(p) for p in profiles) or (self.roles and not set(profiles) <= set(self.roles)):
            self.error = "Perfil desconhecido."
            return
        if len(profiles) > MAX_ROLES:
            self.error = f"No máximo {MAX_ROLES} papéis ativos."
            return
        self.set_selected(profiles)
        self.error = ""

    def set_selected(self, profiles: list[str]) -> None:
        self.selected = profiles
        (STATE / "selected.tmp").write_text("\n".join(profiles))
        (STATE / "selected.tmp").replace(STATE / "selected")
        self.write_profiles()

    def write_profiles(self) -> None:
        """Um perfil por papel ativo. Os nomes têm alfabeto fechado
        (`PROFILE_RE`), então vão no INI e na linha de comando sem aspas."""
        lines = ["# Gerenciado pelo KrewHub (extensão aws-sso); não edite.\n"]
        for i, p in enumerate(self.selected):
            flag = " --default" if i == 0 else ""
            lines.append(f"\n[profile {p}]\ncredential_process = python3 {HELPER} {p}{flag}\n")
        content = "".join(lines)
        if content == self.profiles_written:
            return
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PROFILES_DIR / "config.tmp"
        tmp.write_text(content)
        tmp.replace(PROFILES_DIR / "config")
        self.profiles_written = content

    def mode(self, profile: str) -> str:
        return "default" if self.selected and self.selected[0] == profile else "slot"

    def unload(self, profile: str, mode: str) -> None:
        """Remove o slot nomeado do servidor. O padrão nunca é apagado: no
        aws-sso 2.3.2 `DELETE /` deixa o servidor em pânico no próximo
        `GET /` (e `aws-sso ecs unload` também quebra), então ele só é
        substituído pelo próximo `ecs load`."""
        if mode == "slot":
            delete_slot(profile)
        self.loaded.pop(profile, None)

    def load_creds(self, profile: str, *, refresh: bool) -> None:
        mode = self.mode(profile)
        args = ["ecs", "load", "--profile", profile, "--server", f"localhost:{PORT}"]
        if mode == "slot":
            args.append("--slotted")
        if refresh:
            args.append("--sts-refresh")
        res = run(args)
        if res.returncode == 0:
            self.loaded[profile] = mode
            self.last_load = time.time()
        else:
            log(f"ecs load falhou: rc={res.returncode}")
            self.loaded.pop(profile, None)

    def sync_slots(self, *, refresh: bool, force: bool) -> None:
        """Leva o servidor ao conjunto desejado: descarrega o que saiu (ou
        trocou de slot pra padrão), carrega o que falta ou mudou de modo
        (repetindo a cada `RETRY_SECONDS` se falhar; `force` ignora a espera)
        e, com `refresh`/vencimento, renova o resto."""
        for profile, mode in list(self.loaded.items()):
            if profile not in self.selected or (mode == "slot" and self.mode(profile) == "default"):
                self.unload(profile, mode)
        due = bool(self.loaded) and time.time() - self.last_load > RELOAD_SECONDS
        pending = [p for p in self.selected if self.loaded.get(p) != self.mode(p)]
        if not force and time.time() - self.last_attempt < RETRY_SECONDS:
            pending = []
        for p in self.selected:
            if p in pending:
                self.load_creds(p, refresh=False)
            elif p in self.loaded and (refresh or due):
                self.load_creds(p, refresh=True)
        if pending or refresh or due:
            self.last_attempt = time.time()
        if any(p not in self.loaded for p in self.selected):
            self.error = "Não foi possível carregar as credenciais de um ou mais papéis."
        elif self.error.startswith("Não foi possível carregar"):
            self.error = ""

    # --- ciclo ------------------------------------------------------------

    def tick(self) -> None:
        server_up = self.ensure_server()
        logged_in = (STATE / "login_ok").exists()

        if not logged_in:
            self.drop_session()
            take(STATE / "req" / "refresh")
            take(STATE / "req" / "reload")
            take(STATE / "req" / "profiles")
        else:
            if take(STATE / "req" / "refresh") is not None or not self.roles_fetched:
                self.refresh_roles()
                logged_in = (STATE / "login_ok").exists()
            if logged_in:
                wanted = take(STATE / "req" / "profiles")
                changed = wanted is not None
                if wanted is not None:
                    self.apply_selection(wanted)
                elif not self.selected and len(self.roles) == 1:
                    self.set_selected([self.roles[0]])
                    changed = True
                reload = take(STATE / "req" / "reload") is not None
                if self.selected and server_up:
                    self.sync_slots(refresh=reload, force=changed or reload)

        self.write_status(server_up, logged_in)

    def write_status(self, server_up: bool, logged_in: bool) -> None:
        names = self.roles[:MAX_LISTED]
        loaded = [p for p in self.selected if p in self.loaded] if server_up else []
        data = {
            "ts": time.time(),
            "server": server_up,
            "logged_in": logged_in,
            "profile": self.selected[0] if self.selected else "",
            "profiles": self.selected,
            "loaded_profiles": loaded,
            "loaded": bool(self.selected) and len(loaded) == len(self.selected),
            "roles": len(self.roles),
            "role_names": names,
            "role_labels": {p: self.labels.get(p, "") for p in dict.fromkeys([*self.selected, *names])},
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
