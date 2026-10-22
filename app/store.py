"""Persistência (owner_id, profile) -> {namespace, host, status} em SQLite
-- MVP explícito no plano: nada de operator/CRD nesta fatia. Um arquivo,
sem servidor externo, suficiente pra rastrear o que o reconcile já criou.

Dimensão `profile` (ver AGENTS.md, seção "Multi-profile"): um mesmo
`owner_id` pode ter N sessões de dev provisionadas EM PARALELO (ex.:
"Home" e "Work"), cada uma totalmente isolada (namespace/slug/host,
extensões e geração de sessão próprios). O profile `default` reproduz
EXATAMENTE o comportamento single-session de antes desta fatia -- toda
linha pré-migração é mapeada pra ele, e o slug do profile `default` é
idêntico ao slug de antes (ver app/k8s_templates.py::slugify), pra que os
Pods/PVCs/Secrets já existentes no cluster continuem casando pelos mesmos
nomes determinísticos sem recriação."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

# Profile default -- um owner_id sem profile explícito (fluxo antigo,
# token legado, linha pré-migração) cai aqui, e o comportamento é
# bit-a-bit o de antes desta fatia.
DEFAULT_PROFILE = "default"

SCHEMA = """
CREATE TABLE IF NOT EXISTS devs (
    owner_id   TEXT NOT NULL,
    profile    TEXT NOT NULL DEFAULT 'default',
    slug       TEXT NOT NULL,
    namespace  TEXT NOT NULL,
    host       TEXT NOT NULL,
    status     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner_id, profile)
);
"""

# Contador de geracao de sessao por (owner_id, profile) -- ver app/auth.py
# (payload do token ganha "gen" + "profile") e _verify_session_checked/
# logout em app/main.py. /logout de UM profile incrementa SÓ a geracao
# daquele profile; qualquer token assinado ANTES do incremento pra esse
# mesmo profile passa a falhar a verificacao, mesmo com assinatura e
# expiracao ainda validas -- e o que da revogacao de verdade pro
# krewhub_session (issue #2), agora POR profile (um /logout no "Work" nao
# desloga o "Home"). Tabela PROPRIA, nunca uma coluna em `devs`: /logout
# roda tambem pra (owner_id, profile) nunca provisionado, e toda checagem
# de "nao provisionado" em app/main.py e `store.get(...) is None` -- criar
# uma linha em `devs` so pra guardar o contador faria esse par parecer
# provisionado (namespace/slug vazios). Sem linha = geracao 0.
SESSION_GENERATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_generations (
    owner_id   TEXT NOT NULL,
    profile    TEXT NOT NULL DEFAULT 'default',
    generation INTEGER NOT NULL,
    PRIMARY KEY (owner_id, profile)
);
"""

# Estado por (dev, profile, extensão). `config_json` guarda a config NÃO
# secreta e, sob a chave reservada `__secrets__`, só marcadores
# `{chave: {"set": bool, "updated_at": iso}}` -- o valor de um campo
# secret/generated NUNCA passa por aqui, vive só no Secret
# `krewhub-ext-<slug>` do k8s (o `slug` já carrega o profile, ver
# app/k8s_templates.py::slugify). `runtime_state_json` é o dict que os
# hooks da extensão leem/escrevem (`ExtensionContext.state`); é descartado
# em /logout e /close.
DEV_EXTENSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS dev_extensions (
    owner_id           TEXT NOT NULL,
    profile            TEXT NOT NULL DEFAULT 'default',
    ext_id             TEXT NOT NULL,
    enabled            INTEGER NOT NULL DEFAULT 0,
    config_json        TEXT NOT NULL DEFAULT '{}',
    runtime_state_json TEXT NOT NULL DEFAULT '{}',
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (owner_id, profile, ext_id)
);
"""

SECRETS_KEY = "__secrets__"

# Migração leve pra bancos já criados antes destas colunas existirem --
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


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def _migrate_profile_dimension(conn: sqlite3.Connection) -> None:
    """Adiciona a coluna/PK `profile` nas 3 tabelas de bancos criados
    ANTES desta fatia (PK era só `owner_id`, ou `(owner_id, ext_id)`).

    SQLite não deixa alterar a PRIMARY KEY nem adicionar uma coluna NOT
    NULL a uma PK composta via `ALTER TABLE` -- a forma suportada é a
    "table rebuild" da própria doc do SQLite: cria a tabela nova com o
    schema definitivo (`CREATE TABLE IF NOT EXISTS ...`, que já traz a
    coluna `profile` e a PK composta), copia as linhas antigas
    carimbando `profile='default'`, e troca o nome. Preservar as linhas
    antigas mapeadas pro profile `default` é o que mantém o
    comportamento single-session 100% backward-compatible (o slug do
    profile `default` é idêntico ao slug de antes -- ver
    app/k8s_templates.py).

    Idempotente: se a coluna `profile` já existe em TODAS as tabelas, não
    faz nada (rodar `connect()` de novo não re-migra). A detecção é por
    tabela -- um banco meio-migrado (coluna numa tabela, não noutra)
    termina a migração das que faltam."""
    # As três tabelas-alvo, com o schema definitivo e as colunas a copiar
    # (fora `profile`, que é carimbado). As colunas copiadas são lidas da
    # tabela antiga pra não assumir que todas as migrações de coluna
    # (`last_token_issued_at`, `login_*`) já rodaram.
    targets = (
        ("devs", SCHEMA),
        ("session_generations", SESSION_GENERATIONS_SCHEMA),
        ("dev_extensions", DEV_EXTENSIONS_SCHEMA),
    )
    for table, _schema in targets:
        # Tabela inexistente (banco novo) -> o CREATE em `connect()` já a
        # cria com `profile`; nada a migrar. Tabela já com `profile` ->
        # idem. Só migra a que existe SEM `profile`.
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if existing is None or _table_has_column(conn, table, "profile"):
            continue

        old_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        tmp = f"{table}__profile_migration"
        conn.execute(f"DROP TABLE IF EXISTS {tmp}")
        # Cria a tabela nova com o nome temporário, aplicando o schema
        # definitivo (o schema usa o nome real -- troca via replace do
        # primeiro match, suficiente: o nome da tabela aparece só no
        # CREATE TABLE).
        conn.execute(_schema.replace(table, tmp, 1))
        # A tabela antiga pode ter colunas que o schema base NÃO declara
        # (migrações de coluna já rodadas antes: `last_token_issued_at`,
        # `login_*`). Acrescenta cada uma que falta ao tmp ANTES de copiar,
        # pra preservar os dados dessas colunas (senão o INSERT falha ou
        # perderia a coluna). TEXT NULLABLE é suficiente -- é o tipo com
        # que `_MIGRATIONS` as adiciona.
        tmp_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tmp})").fetchall()}
        for col in old_cols:
            if col not in tmp_cols:
                conn.execute(f"ALTER TABLE {tmp} ADD COLUMN {col} TEXT")
        cols_csv = ", ".join(old_cols)
        conn.execute(
            f"INSERT INTO {tmp} ({cols_csv}, profile) "
            f"SELECT {cols_csv}, 'default' FROM {table}"
        )
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")


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
    _migrate_profile_dimension(conn)
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
    profile: str = DEFAULT_PROFILE,
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO devs (owner_id, profile, slug, namespace, host, status, detail, created_at, updated_at)
        VALUES (:owner_id, :profile, :slug, :namespace, :host, :status, :detail, :now, :now)
        ON CONFLICT(owner_id, profile) DO UPDATE SET
            slug=excluded.slug,
            namespace=excluded.namespace,
            host=excluded.host,
            status=excluded.status,
            detail=excluded.detail,
            updated_at=excluded.updated_at
        """,
        {
            "owner_id": owner_id,
            "profile": profile,
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
    profile: str = DEFAULT_PROFILE,
) -> None:
    """Persiste a escolha feita no lobby. Faz upsert de uma linha mínima
    se `(owner_id, profile)` ainda não existe (lobby roda ANTES do
    provision) -- as colunas de infra (namespace/host/status) ficam
    vazias até o provision real rodar e chamar `upsert()`."""
    now = _now()
    conn.execute(
        """
        INSERT INTO devs (
            owner_id, profile, slug, namespace, host, status, detail,
            created_at, updated_at, login_mode, login_identity_provider, login_region
        )
        VALUES (:owner_id, :profile, '', '', '', 'lobby_pending', '', :now, :now, :mode, :ip, :region)
        ON CONFLICT(owner_id, profile) DO UPDATE SET
            login_mode=excluded.login_mode,
            login_identity_provider=excluded.login_identity_provider,
            login_region=excluded.login_region,
            updated_at=excluded.updated_at
        """,
        {
            "owner_id": owner_id,
            "profile": profile,
            "mode": mode,
            "ip": identity_provider,
            "region": region,
            "now": now,
        },
    )
    conn.commit()


def get(conn: sqlite3.Connection, owner_id: str, profile: str = DEFAULT_PROFILE) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM devs WHERE owner_id = ? AND profile = ?", (owner_id, profile)
    ).fetchone()


def list_profiles(conn: sqlite3.Connection, owner_id: str) -> list[sqlite3.Row]:
    """Todas as linhas de `devs` deste owner_id, uma por profile,
    ordenadas por profile. É a fonte da lista "quais sessões este dev tem
    provisionadas" (lobby de profiles, ver app/main.py). Inclui linhas em
    qualquer status (o chamador filtra `closed`/`lobby_pending` se
    quiser)."""
    return conn.execute(
        "SELECT * FROM devs WHERE owner_id = ? ORDER BY profile", (owner_id,)
    ).fetchall()


def mark_token_issued(conn: sqlite3.Connection, owner_id: str, profile: str = DEFAULT_PROFILE) -> None:
    conn.execute(
        "UPDATE devs SET last_token_issued_at = ?, updated_at = ? WHERE owner_id = ? AND profile = ?",
        (_now(), _now(), owner_id, profile),
    )
    conn.commit()


def get_session_generation(
    conn: sqlite3.Connection, owner_id: str, profile: str = DEFAULT_PROFILE
) -> int:
    """Geracao atual de sessao pro (owner_id, profile) -- 0 se ainda nao
    ha linha em `session_generations` (par que nunca fez /logout)."""
    row = conn.execute(
        "SELECT generation FROM session_generations WHERE owner_id = ? AND profile = ?",
        (owner_id, profile),
    ).fetchone()
    return int(row["generation"]) if row is not None else 0


def bump_session_generation(
    conn: sqlite3.Connection, owner_id: str, profile: str = DEFAULT_PROFILE
) -> int:
    """Incrementa a geracao de sessao do (owner_id, profile) (revoga TODO
    token assinado antes desta chamada PARA ESSE profile) e devolve o
    novo valor. Upsert so em `session_generations` -- nunca toca em
    `devs`, entao chamar isto pra um par nunca provisionado nao o faz
    parecer provisionado. Revogacao e POR profile: um /logout no "Work"
    nao mexe na geracao do "Home"."""
    conn.execute(
        """
        INSERT INTO session_generations (owner_id, profile, generation)
        VALUES (?, ?, 1)
        ON CONFLICT(owner_id, profile) DO UPDATE SET
            generation = session_generations.generation + 1
        """,
        (owner_id, profile),
    )
    conn.commit()
    return get_session_generation(conn, owner_id, profile)


def _decode_extension(row: sqlite3.Row) -> dict:
    config = json.loads(row["config_json"])
    secrets = config.pop(SECRETS_KEY, {})
    return {
        "owner_id": row["owner_id"],
        "profile": row["profile"],
        "ext_id": row["ext_id"],
        "enabled": bool(row["enabled"]),
        "config": config,
        "secrets": secrets,
        "runtime_state": json.loads(row["runtime_state_json"]),
        "updated_at": row["updated_at"],
    }


def get_extension(
    conn: sqlite3.Connection, owner_id: str, ext_id: str, profile: str = DEFAULT_PROFILE
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM dev_extensions WHERE owner_id = ? AND profile = ? AND ext_id = ?",
        (owner_id, profile, ext_id),
    ).fetchone()
    return _decode_extension(row) if row is not None else None


def list_extensions(
    conn: sqlite3.Connection, owner_id: str, profile: str = DEFAULT_PROFILE
) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT * FROM dev_extensions WHERE owner_id = ? AND profile = ? ORDER BY ext_id",
        (owner_id, profile),
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
    profile: str = DEFAULT_PROFILE,
) -> None:
    """Atualização parcial: argumento `None` mantém o valor atual (linha
    nova começa desabilitada, sem config, sem estado). `secrets` são os
    marcadores `{chave: {"set": bool, "updated_at": iso}}`."""
    current = get_extension(conn, owner_id, ext_id, profile) or {
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
        INSERT INTO dev_extensions (owner_id, profile, ext_id, enabled, config_json, runtime_state_json, updated_at)
        VALUES (:owner_id, :profile, :ext_id, :enabled, :config, :state, :now)
        ON CONFLICT(owner_id, profile, ext_id) DO UPDATE SET
            enabled=excluded.enabled,
            config_json=excluded.config_json,
            runtime_state_json=excluded.runtime_state_json,
            updated_at=excluded.updated_at
        """,
        {
            "owner_id": owner_id,
            "profile": profile,
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
    conn: sqlite3.Connection,
    owner_id: str,
    ext_id: str,
    *,
    drop_secret_keys: set[str] | None = None,
    profile: str = DEFAULT_PROFILE,
) -> None:
    """Descarta o estado de runtime e (opcional) os marcadores de secrets
    cujas chaves foram apagadas do Secret do k8s (`None` = nenhum;
    passe o conjunto de chaves apagadas). Não toca em `enabled`/config."""
    current = get_extension(conn, owner_id, ext_id, profile)
    if current is None:
        return
    secrets = {
        k: v for k, v in current["secrets"].items() if k not in (drop_secret_keys or set())
    }
    upsert_extension(conn, owner_id, ext_id, secrets=secrets, runtime_state={}, profile=profile)
