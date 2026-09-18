"""Persistência owner_id -> {namespace, host, status} em SQLite -- MVP
explícito no plano: nada de operator/CRD nesta fatia. Um arquivo, sem
servidor externo, suficiente pra rastrear o que o reconcile já criou."""

from __future__ import annotations

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


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM devs ORDER BY created_at").fetchall()
