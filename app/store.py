"""Persistência owner_id -> {namespace, host, status} em SQLite -- MVP
explícito no plano: nada de operator/CRD nesta fatia. Um arquivo, sem
servidor externo, suficiente pra rastrear o que o reconcile já criou."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS devs (
    owner_id   TEXT PRIMARY KEY,
    slug       TEXT NOT NULL,
    namespace  TEXT NOT NULL,
    host       TEXT NOT NULL,
    status     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Contador de geracao de sessao por owner_id -- ver app/auth.py (payload
# do token ganha um campo "gen") e _verify_session_checked/logout em
# app/main.py. /logout incrementa; qualquer token assinado ANTES do
# incremento passa a falhar a verificacao (geracao esperada != geracao
# no token), mesmo com assinatura e expiracao ainda validas -- e o que
# da revogacao de verdade pro krewhub_session (issue #2), sem depender
# de introspection no IdP. Tabela PROPRIA, nunca uma coluna em `devs`:
# /logout roda tambem pra owner_id nunca provisionado, e toda checagem
# de "nao provisionado" em app/main.py e `store.get(...) is None` --
# criar uma linha em `devs` so pra guardar o contador faria esse owner
# parecer provisionado (namespace/slug vazios). Sem linha = geracao 0.
SESSION_GENERATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_generations (
    owner_id   TEXT PRIMARY KEY,
    generation INTEGER NOT NULL
);
"""

# Estado por (dev, extensão). `config_json` guarda a config NÃO secreta e,
# sob a chave reservada `__secrets__`, só marcadores `{chave: {"set": bool,
# "updated_at": iso}}` -- o valor de um campo secret/generated NUNCA passa
# por aqui, vive só no Secret `krewhub-ext-<slug>` do k8s.
# `runtime_state_json` é o dict que os hooks da extensão leem/escrevem
# (`ExtensionContext.state`); é descartado em /logout e /close.
DEV_EXTENSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS dev_extensions (
    owner_id           TEXT NOT NULL,
    ext_id             TEXT NOT NULL,
    enabled            INTEGER NOT NULL DEFAULT 0,
    config_json        TEXT NOT NULL DEFAULT '{}',
    runtime_state_json TEXT NOT NULL DEFAULT '{}',
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (owner_id, ext_id)
);
"""

SECRETS_KEY = "__secrets__"

# Migração leve pra bancos já criados antes desta coluna existir --
# CREATE TABLE IF NOT EXISTS não adiciona coluna em tabela já existente.
# NUNCA guarda o token de sessão em si (é credencial) -- só quando foi
# emitido, pra saber se vale a pena reemitir sem precisar perguntar ao
# kirocrew.
_MIGRATIONS = (
    "ALTER TABLE devs ADD COLUMN last_token_issued_at TEXT",
    # Escolhas feitas no lobby (GET/POST /devs/{owner_id}/lobby) --
    # persistidas ANTES do provision rodar, pra poder encadear o
    # /kiro-login automaticamente com os mesmos valores que o dev
    # escolheu no form, sem pedir de novo.
    "ALTER TABLE devs ADD COLUMN login_mode TEXT",
    "ALTER TABLE devs ADD COLUMN login_identity_provider TEXT",
    "ALTER TABLE devs ADD COLUMN login_region TEXT",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    conn.execute(SESSION_GENERATIONS_SCHEMA)
    conn.execute(DEV_EXTENSIONS_SCHEMA)
    for migration in _MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def upsert(
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    slug: str,
    namespace: str,
    host: str,
    status: str,
    detail: str = "",
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO devs (owner_id, slug, namespace, host, status, detail, created_at, updated_at)
        VALUES (:owner_id, :slug, :namespace, :host, :status, :detail, :now, :now)
        ON CONFLICT(owner_id) DO UPDATE SET
            slug=excluded.slug,
            namespace=excluded.namespace,
            host=excluded.host,
            status=excluded.status,
            detail=excluded.detail,
            updated_at=excluded.updated_at
        """,
        {
            "owner_id": owner_id,
            "slug": slug,
            "namespace": namespace,
            "host": host,
            "status": status,
            "detail": detail,
            "now": now,
        },
    )
    conn.commit()


def set_login_choice(
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    mode: str,
    identity_provider: str = "",
    region: str = "",
) -> None:
    """Persiste a escolha feita no lobby. Faz upsert de uma linha mínima
    se `owner_id` ainda não existe (lobby roda ANTES do provision) --
    as colunas de infra (namespace/host/status) ficam vazias até o
    provision real rodar e chamar `upsert()`."""
    now = _now()
    conn.execute(
        """
        INSERT INTO devs (
            owner_id, slug, namespace, host, status, detail,
            created_at, updated_at, login_mode, login_identity_provider, login_region
        )
        VALUES (:owner_id, '', '', '', 'lobby_pending', '', :now, :now, :mode, :ip, :region)
        ON CONFLICT(owner_id) DO UPDATE SET
            login_mode=excluded.login_mode,
            login_identity_provider=excluded.login_identity_provider,
            login_region=excluded.login_region,
            updated_at=excluded.updated_at
        """,
        {
            "owner_id": owner_id,
            "mode": mode,
            "ip": identity_provider,
            "region": region,
            "now": now,
        },
    )
    conn.commit()


def get(conn: sqlite3.Connection, owner_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM devs WHERE owner_id = ?", (owner_id,)).fetchone()


def mark_token_issued(conn: sqlite3.Connection, owner_id: str) -> None:
    conn.execute(
        "UPDATE devs SET last_token_issued_at = ?, updated_at = ? WHERE owner_id = ?",
        (_now(), _now(), owner_id),
    )
    conn.commit()


def get_session_generation(conn: sqlite3.Connection, owner_id: str) -> int:
    """Geracao atual de sessao pro owner_id -- 0 se ainda nao ha linha
    em `session_generations` (owner_id que nunca fez /logout)."""
    row = conn.execute(
        "SELECT generation FROM session_generations WHERE owner_id = ?", (owner_id,)
    ).fetchone()
    return int(row["generation"]) if row is not None else 0


def bump_session_generation(conn: sqlite3.Connection, owner_id: str) -> int:
    """Incrementa a geracao de sessao do owner_id (revoga TODO token
    assinado antes desta chamada) e devolve o novo valor. Upsert so em
    `session_generations` -- nunca toca em `devs`, entao chamar isto
    pra um owner_id nunca provisionado nao o faz parecer provisionado."""
    conn.execute(
        """
        INSERT INTO session_generations (owner_id, generation)
        VALUES (?, 1)
        ON CONFLICT(owner_id) DO UPDATE SET
            generation = session_generations.generation + 1
        """,
        (owner_id,),
    )
    conn.commit()
    return get_session_generation(conn, owner_id)


def _decode_extension(row: sqlite3.Row) -> dict:
    config = json.loads(row["config_json"])
    secrets = config.pop(SECRETS_KEY, {})
    return {
        "owner_id": row["owner_id"],
        "ext_id": row["ext_id"],
        "enabled": bool(row["enabled"]),
        "config": config,
        "secrets": secrets,
        "runtime_state": json.loads(row["runtime_state_json"]),
        "updated_at": row["updated_at"],
    }


def get_extension(conn: sqlite3.Connection, owner_id: str, ext_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM dev_extensions WHERE owner_id = ? AND ext_id = ?", (owner_id, ext_id)
    ).fetchone()
    return _decode_extension(row) if row is not None else None


def list_extensions(conn: sqlite3.Connection, owner_id: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT * FROM dev_extensions WHERE owner_id = ? ORDER BY ext_id", (owner_id,)
    ).fetchall()
    return {r["ext_id"]: _decode_extension(r) for r in rows}


def upsert_extension(
    conn: sqlite3.Connection,
    owner_id: str,
    ext_id: str,
    *,
    enabled: bool | None = None,
    config: dict | None = None,
    secrets: dict | None = None,
    runtime_state: dict | None = None,
) -> None:
    """Atualização parcial: argumento `None` mantém o valor atual (linha
    nova começa desabilitada, sem config, sem estado). `secrets` são os
    marcadores `{chave: {"set": bool, "updated_at": iso}}`."""
    current = get_extension(conn, owner_id, ext_id) or {
        "enabled": False,
        "config": {},
        "secrets": {},
        "runtime_state": {},
    }
    new_config = dict(current["config"] if config is None else config)
    new_secrets = current["secrets"] if secrets is None else secrets
    if new_secrets:
        new_config[SECRETS_KEY] = new_secrets
    conn.execute(
        """
        INSERT INTO dev_extensions (owner_id, ext_id, enabled, config_json, runtime_state_json, updated_at)
        VALUES (:owner_id, :ext_id, :enabled, :config, :state, :now)
        ON CONFLICT(owner_id, ext_id) DO UPDATE SET
            enabled=excluded.enabled,
            config_json=excluded.config_json,
            runtime_state_json=excluded.runtime_state_json,
            updated_at=excluded.updated_at
        """,
        {
            "owner_id": owner_id,
            "ext_id": ext_id,
            "enabled": int(current["enabled"] if enabled is None else enabled),
            "config": json.dumps(new_config),
            "state": json.dumps(current["runtime_state"] if runtime_state is None else runtime_state),
            "now": _now(),
        },
    )
    conn.commit()


def secret_marker() -> dict:
    return {"set": True, "updated_at": _now()}


def reset_extension_runtime(
    conn: sqlite3.Connection, owner_id: str, ext_id: str, *, drop_secret_keys: set[str] | None = None
) -> None:
    """Descarta o estado de runtime e (opcional) os marcadores de secrets
    cujas chaves foram apagadas do Secret do k8s (`None` = nenhum;
    passe o conjunto de chaves apagadas). Não toca em `enabled`/config."""
    current = get_extension(conn, owner_id, ext_id)
    if current is None:
        return
    secrets = {
        k: v for k, v in current["secrets"].items() if k not in (drop_secret_keys or set())
    }
    upsert_extension(conn, owner_id, ext_id, secrets=secrets, runtime_state={})
