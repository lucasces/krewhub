"""Orquestração das extensões em runtime: planos pro reconcile, estados e
condições, ações, config do form, limpeza de secrets e token anti-CSRF.

O core nunca confia na extensão pra isolar falhas: qualquer exceção de
hook vira cartão `error` (status), `ActionResult(ok=False)` (ação) ou
log (`on_pod_ready`) -- uma extensão quebrada não derruba o lobby nem o
provision dos outros."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from kubernetes.client.rest import ApiException

from app import extensions as registry
from app import k8s_manager, pod_exec, store
from app import k8s_templates as tpl
from app.config import Settings
from app.extensions.base import (
    ActionResult,
    BuildContext,
    Card,
    Extension,
    ExtensionContext,
    ExtensionError,
    FieldSpec,
    Status,
    secret_key,
    secret_name,
)
from app.extensions.contributions import ExtPlan
from app.extensions.ui import ActionButton, CardView, field_name

logger = logging.getLogger("krewhub.extensions")

LAST_ACTION_KEY = "__last_action__"
_TRUE = ("1", "true", "on", "yes")


# ---------------------------------------------------------------------------
# Config do form
# ---------------------------------------------------------------------------


def _merged_config(ext: Extension, stored: Mapping[str, Any]) -> dict[str, Any]:
    cfg = {f.key: f.default for f in ext.fields if f.kind not in ("secret", "generated")}
    cfg.update(stored)
    return cfg


def parse_form(
    settings: Settings, owner_id: str, form: Mapping[str, str]
) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """Lê os campos `ext.<id>.<chave>` do form do lobby. Devolve
    `(mudanças, erros)`: `mudanças[ext_id] = {enabled, config, secret_values}`
    só pras extensões habilitadas pelo admin; `erros[ext_id]` lista as
    mensagens de validação (só quando a extensão está sendo habilitada)."""
    exts = registry.enabled_extensions(settings)
    with store.connect(settings.db_path) as conn:
        rows = store.list_extensions(conn, owner_id)

    changes: dict[str, dict] = {}
    errors: dict[str, list[str]] = {}
    for ext_id, ext in exts.items():
        enabled = str(form.get(field_name(ext_id, "enabled"), "")).lower() in _TRUE
        row = rows.get(ext_id) or {"config": {}, "secrets": {}}
        config: dict[str, Any] = {}
        secret_values: dict[str, str] = {}
        for f in ext.fields:
            raw = form.get(field_name(ext_id, f.key))
            if f.kind == "generated":
                continue
            if f.kind == "secret":
                if raw and raw.strip():
                    secret_values[secret_key(ext_id, f.key)] = raw.strip()
                continue
            if f.kind == "bool":
                config[f.key] = str(raw or "").lower() in _TRUE
            else:
                config[f.key] = (raw if raw is not None else row["config"].get(f.key, f.default)).strip()
        if enabled:
            msgs = list(ext.validate(_merged_config(ext, config)))
            for f in ext.fields:
                if (
                    f.kind == "secret"
                    and f.required
                    and not secret_values.get(secret_key(ext_id, f.key))
                    and not (row["secrets"].get(f.key) or {}).get("set")
                ):
                    msgs.append(f"{f.label or f.key}: obrigatório")
            if msgs:
                errors[ext_id] = msgs
        changes[ext_id] = {"enabled": enabled, "config": config, "secret_values": secret_values}
    return changes, errors


def save_form(settings: Settings, owner_id: str, changes: Mapping[str, dict]) -> None:
    """Persiste o que `parse_form` leu: config no SQLite, valores de
    campos `secret` direto no Secret do k8s (só marcadores no SQLite) e,
    ao DESABILITAR uma extensão, apaga as chaves dela do Secret."""
    if not changes:
        return
    slug = tpl.slugify(owner_id)
    namespace = settings.dev_namespace
    with store.connect(settings.db_path) as conn:
        rows = store.list_extensions(conn, owner_id)
        needs_k8s = any(
            ch["secret_values"] or (rows.get(i, {}).get("enabled") and not ch["enabled"])
            for i, ch in changes.items()
        )
        c = k8s_manager.get_clients(settings) if needs_k8s else None
        for ext_id, ch in changes.items():
            prev = rows.get(ext_id)
            secrets = dict(prev["secrets"]) if prev else {}
            if ch["secret_values"] and c is not None:
                k8s_manager.ensure_ext_secret(c, namespace, slug, set_values=ch["secret_values"])
                for full_key in ch["secret_values"]:
                    secrets[full_key.split(".", 1)[1]] = store.secret_marker()
            if prev and prev["enabled"] and not ch["enabled"] and c is not None:
                k8s_manager.wipe_ext_secret_keys(c, namespace, slug, prefix=f"{ext_id}.")
                secrets = {}
                store.upsert_extension(conn, owner_id, ext_id, runtime_state={})
            store.upsert_extension(
                conn, owner_id, ext_id, enabled=ch["enabled"], config=ch["config"], secrets=secrets
            )


# ---------------------------------------------------------------------------
# Planos pro reconcile
# ---------------------------------------------------------------------------


def active_extensions(settings: Settings, owner_id: str) -> list[tuple[Extension, dict]]:
    """Habilitadas pelo admin E ligadas pelo dev, com config válida."""
    exts = registry.enabled_extensions(settings)
    if not exts:
        return []
    with store.connect(settings.db_path) as conn:
        rows = store.list_extensions(conn, owner_id)
    active = []
    for ext_id, ext in exts.items():
        row = rows.get(ext_id)
        if not row or not row["enabled"]:
            continue
        config = _merged_config(ext, row["config"])
        if ext.validate(config):
            logger.warning("extensão %r com config inválida pra owner=%s -- ignorada", ext_id, owner_id)
            continue
        active.append((ext, config))
    return active


def build_plans(settings: Settings, owner_id: str) -> tuple[ExtPlan, ...]:
    slug = tpl.slugify(owner_id)
    plans = []
    for ext, config in active_extensions(settings, owner_id):
        contribution = ext.pod_contribution(
            BuildContext(
                owner_id=owner_id,
                slug=slug,
                namespace=settings.dev_namespace,
                config=config,
                settings=settings,
            )
        )
        generated = tuple(secret_key(ext.id, f.key) for f in ext.fields if f.kind == "generated")
        plans.append(ExtPlan(ext.id, contribution, generated))
    return tuple(plans)


# ---------------------------------------------------------------------------
# Contexto + condições
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PodSnapshot:
    name: str | None
    ready: bool
    running_containers: frozenset[str]


def pod_snapshot(c: k8s_manager.Clients, namespace: str, slug: str) -> PodSnapshot:
    name = f"kirocrew-{slug}"
    try:
        pod = c.core.read_namespaced_pod(name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return PodSnapshot(None, False, frozenset())
        raise
    statuses = getattr(pod.status, "container_statuses", None) or []
    running = frozenset(
        s.name for s in statuses if getattr(getattr(s, "state", None), "running", None) is not None
    )
    main_ready = pod.status.phase == "Running" and any(
        s.name == pod_exec.MAIN_CONTAINER and s.ready for s in statuses
    )
    return PodSnapshot(name, bool(main_ready), running)


def _base_conditions(ext: Extension, snap: PodSnapshot) -> dict[str, bool]:
    conds = {"pod.ready": snap.ready}
    conds[f"sidecar.{ext.sidecar_name}.running"] = ext.sidecar_name in snap.running_containers
    return conds


def _make_context(
    settings: Settings,
    c: k8s_manager.Clients,
    owner_id: str,
    slug: str,
    ext: Extension,
    row: Mapping,
    config: Mapping,
    snap: PodSnapshot,
) -> ExtensionContext:
    namespace = settings.dev_namespace
    pod_name = snap.name or f"kirocrew-{slug}"

    def _exec(container: str, script: str) -> str:
        return pod_exec.exec_sh(c, pod_name, namespace, script, container=container)

    def _get_secret(key: str) -> str | None:
        try:
            sec = c.core.read_namespaced_secret(secret_name(slug), namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        raw = (getattr(sec, "data", None) or {}).get(secret_key(ext.id, key))
        return base64.b64decode(raw).decode() if raw else None

    return ExtensionContext(
        owner_id=owner_id,
        slug=slug,
        namespace=namespace,
        pod_name=pod_name,
        config=config,
        state=dict(row.get("runtime_state", {})),
        base_conditions=_base_conditions(ext, snap),
        settings=settings,
        container_default=ext.sidecar_name,
        _exec=_exec,
        _get_secret=_get_secret,
        _run_detached=lambda container, command, tag, script, markers, timeout: pod_exec.run_detached(
            c,
            pod_name,
            namespace,
            pod_exec.DetachedFlow(
                container=container,
                command=command,
                tag=tag,
                script=script,
                done_markers=markers,
                stage_timeout=timeout,
            ),
        ),
    )


def _derive(conditions: Mapping[str, bool]) -> str:
    return "ready" if all(conditions.values()) else "needs_action"


def _persist_state(settings: Settings, owner_id: str, ext_id: str, before: Mapping, after: Mapping) -> None:
    if dict(before) != dict(after):
        with store.connect(settings.db_path) as conn:
            store.upsert_extension(conn, owner_id, ext_id, runtime_state=dict(after))


@dataclasses.dataclass
class ExtView:
    ext_id: str
    name: str
    state: str
    conditions: dict[str, bool]
    card: Card
    actions: tuple[ActionButton, ...]
    last_action: str = ""

    def to_json(self) -> dict:
        return {
            "id": self.ext_id,
            "name": self.name,
            "state": self.state,
            "conditions": self.conditions,
            "summary": self.card.summary,
            "actions": [{"id": a.id, "enabled": a.enabled} for a in self.actions],
        }

    def card_view(self) -> CardView:
        return CardView(self.ext_id, self.name, self.state, self.card, self.actions, self.last_action)


def wants_refresh(views) -> bool:
    """O iframe de cartões recarrega sozinho enquanto o Pod não está pronto
    (`pending`) ou alguma extensão declara que espera algo externo
    (`Card.polling`). `needs_action` sozinho NÃO basta: é também o estado
    estável "o dev precisa escolher algo", e recarregar apagaria o que ele
    está digitando num formulário de ação."""
    return any(v.state == "pending" or v.card.polling for v in views)


def _buttons(ext: Extension, conditions: Mapping[str, bool], *, usable: bool) -> tuple[ActionButton, ...]:
    return tuple(
        ActionButton(
            a.id,
            a.label,
            usable and all(conditions.get(r, False) for r in a.requires),
            a.description,
            a.params,
        )
        for a in ext.actions
    )


def _format_last_action(state: Mapping) -> str:
    last = state.get(LAST_ACTION_KEY)
    if not isinstance(last, dict):
        return ""
    mark = "ok" if last.get("ok") else "falhou"
    return f"Última ação ({last.get('id')}): {mark}. {last.get('message', '')}".strip()


def evaluate(
    settings: Settings, owner_id: str, *, c: k8s_manager.Clients | None = None, only: str | None = None
) -> list[ExtView]:
    """Estado + cartão de cada extensão habilitada pelo admin.
    Desligada pelo dev -> `inactive` (hook nunca chamado); Pod ausente ou
    não pronto -> `pending`; hook levantando -> `error`."""
    exts = registry.enabled_extensions(settings)
    if only is not None:
        exts = {k: v for k, v in exts.items() if k == only}
    if not exts:
        return []
    slug = tpl.slugify(owner_id)
    with store.connect(settings.db_path) as conn:
        rows = store.list_extensions(conn, owner_id)

    views: list[ExtView] = []
    snap: PodSnapshot | None = None
    for ext_id, ext in exts.items():
        row = rows.get(ext_id)
        name = ext.name or ext.id
        if not row or not row["enabled"]:
            views.append(
                ExtView(ext_id, name, "inactive", {}, Card(title=name, state="inactive",
                        summary="Extensão desativada."), ())
            )
            continue
        config = _merged_config(ext, row["config"])
        problems = ext.validate(config)
        if problems:
            views.append(
                ExtView(ext_id, name, "error", {}, Card(title=name, state="error",
                        summary="Configuração inválida.", messages=tuple(problems)), ())
            )
            continue

        c = c or k8s_manager.get_clients(settings)
        try:
            snap = snap or pod_snapshot(c, settings.dev_namespace, slug)
        except Exception:  # noqa: BLE001
            logger.exception("falha lendo o Pod de owner=%s", owner_id)
            snap = PodSnapshot(None, False, frozenset())
        base = _base_conditions(ext, snap)
        if not snap.ready:
            views.append(
                ExtView(ext_id, name, "pending", base, Card(title=name, state="pending",
                        summary="Aguardando o workspace ficar pronto."), _buttons(ext, base, usable=False))
            )
            continue

        ctx = _make_context(settings, c, owner_id, slug, ext, row, config, snap)
        before = dict(ctx.state)
        try:
            status = ext.status(ctx)
            if not isinstance(status, Status):
                raise TypeError("status() precisa devolver Status")
            conditions = {**base, **status.conditions}
            state = status.state or _derive(conditions)
            card = ext.lobby_card(ctx) or status.card
            card = card or Card(title=name)
            card = dataclasses.replace(card, state=state)
        except Exception as exc:  # noqa: BLE001 -- código de extensão
            logger.exception("status() da extensão %r falhou (owner=%s)", ext_id, owner_id)
            msg = str(exc) if isinstance(exc, ExtensionError) else type(exc).__name__
            conditions, state = dict(base), "error"
            card = Card(title=name, state="error", summary="Falha ao consultar o estado.", messages=(msg,))
        _persist_state(settings, owner_id, ext_id, before, ctx.state)
        views.append(
            ExtView(ext_id, name, state, conditions, card, _buttons(ext, conditions, usable=True),
                    _format_last_action(ctx.state))
        )
    return views


# ---------------------------------------------------------------------------
# Ações
# ---------------------------------------------------------------------------


class ActionRejected(Exception):
    """Pedido inválido (extensão/ação desconhecida, desligada, requisito
    não atendido) -- vira 4xx no endpoint."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def run_action(
    settings: Settings, owner_id: str, ext_id: str, action_id: str, form: Mapping[str, str]
) -> ActionResult:
    exts = registry.enabled_extensions(settings)
    ext = exts.get(ext_id)
    if ext is None:
        raise ActionRejected("extensão desconhecida", 404)
    spec = ext.action_map().get(action_id)
    if spec is None:
        raise ActionRejected("ação desconhecida", 404)

    views = evaluate(settings, owner_id, only=ext_id)
    view = views[0] if views else None
    if view is None or view.state in ("inactive", "pending"):
        raise ActionRejected("extensão não está ativa ou o workspace não está pronto", 409)
    button = next((b for b in view.actions if b.id == action_id), None)
    if button is None or not button.enabled:
        raise ActionRejected("pré-requisitos da ação não atendidos", 409)

    params: dict[str, str] = {}
    for p in spec.params:
        value = str(form.get(p.key, p.default)).strip()
        if p.kind == "select" and value not in p.options:
            raise ActionRejected(f"parâmetro {p.key!r} inválido")
        if p.required and not value:
            raise ActionRejected(f"parâmetro {p.key!r} obrigatório")
        params[p.key] = value

    slug = tpl.slugify(owner_id)
    c = k8s_manager.get_clients(settings)
    with store.connect(settings.db_path) as conn:
        row = store.get_extension(conn, owner_id, ext_id) or {}
    snap = pod_snapshot(c, settings.dev_namespace, slug)
    ctx = _make_context(settings, c, owner_id, slug, ext, row, _merged_config(ext, row.get("config", {})), snap)
    before = dict(ctx.state)
    try:
        result = ext.handle_action(ctx, action_id, params)
    except ExtensionError as exc:
        result = ActionResult(ok=False, message=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("ação %s/%s falhou (owner=%s)", ext_id, action_id, owner_id)
        result = ActionResult(ok=False, message=f"falha interna ({type(exc).__name__})")

    ctx.state.update(result.state_updates)
    ctx.state[LAST_ACTION_KEY] = {
        "id": action_id,
        "ok": result.ok,
        "message": result.message,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    _persist_state(settings, owner_id, ext_id, before, ctx.state)
    return result


def run_on_pod_ready(settings: Settings, owner_id: str, *, c: k8s_manager.Clients | None = None) -> None:
    """Melhor esforço: exceção de qualquer extensão só é logada."""
    active = active_extensions(settings, owner_id)
    if not active:
        return
    slug = tpl.slugify(owner_id)
    c = c or k8s_manager.get_clients(settings)
    try:
        snap = pod_snapshot(c, settings.dev_namespace, slug)
    except Exception:  # noqa: BLE001
        logger.exception("on_pod_ready: falha lendo o Pod (owner=%s)", owner_id)
        return
    with store.connect(settings.db_path) as conn:
        rows = store.list_extensions(conn, owner_id)
    for ext, config in active:
        row = rows.get(ext.id) or {}
        ctx = _make_context(settings, c, owner_id, slug, ext, row, config, snap)
        before = dict(ctx.state)
        try:
            ext.on_pod_ready(ctx)
        except Exception:  # noqa: BLE001
            logger.exception("on_pod_ready da extensão %r falhou (owner=%s)", ext.id, owner_id)
        _persist_state(settings, owner_id, ext.id, before, ctx.state)


def on_pod_ready_best_effort(settings: Settings, owner_id: str, c: k8s_manager.Clients) -> None:
    """`run_on_pod_ready` que nunca levanta -- falha de extensão não pode
    quebrar o provision do dev."""
    try:
        run_on_pod_ready(settings, owner_id, c=c)
    except Exception:  # noqa: BLE001
        logger.exception("on_pod_ready falhou (owner=%s)", owner_id)


# ---------------------------------------------------------------------------
# Limpeza de secrets (/logout e /close)
# ---------------------------------------------------------------------------


def wipe_secrets(
    settings: Settings, owner_id: str, *, generated_only: bool, c: k8s_manager.Clients | None = None
) -> list[str]:
    """Remove chaves do Secret `krewhub-ext-<slug>` (merge patch com
    `null`) e zera o estado de runtime + marcadores correspondentes.

    - `/logout` (`generated_only=False`): TODAS as chaves -- o dev está
      saindo, nada dele deve sobrar no cluster.
    - `/close` (`generated_only=True`): só os campos `generated` (tokens
      internos regenerados no próximo provision); o que o dev digitou
      (`secret`) sobrevive pra não precisar redigitar.

    Levanta `ApiException` em erro real; chamadores tratam como melhor
    esforço, igual à revogação de sessão."""
    slug = tpl.slugify(owner_id)
    namespace = settings.dev_namespace
    c = c or k8s_manager.get_clients(settings)

    if generated_only:
        keys = [
            secret_key(ext.id, f.key)
            for ext in registry.discover().values()
            for f in ext.fields
            if f.kind == "generated"
        ]
        wiped = k8s_manager.wipe_ext_secret_keys(c, namespace, slug, keys) if keys else []
    else:
        wiped = k8s_manager.wipe_ext_secret_keys(c, namespace, slug)

    with store.connect(settings.db_path) as conn:
        wiped_set = set(wiped)
        for ext_id, row in store.list_extensions(conn, owner_id).items():
            dropped = {k for k in row["secrets"] if secret_key(ext_id, k) in wiped_set}
            store.reset_extension_runtime(conn, owner_id, ext_id, drop_secret_keys=dropped)
    return wiped


# ---------------------------------------------------------------------------
# CSRF das ações
# ---------------------------------------------------------------------------

CSRF_TTL_SECONDS = 3600


def _csrf_sig(secret: str, owner_id: str, ext_id: str, action_id: str, exp: int) -> str:
    key = hashlib.sha256(b"krewhub-ext-action:" + secret.encode()).digest()
    msg = f"{owner_id}\x00{ext_id}\x00{action_id}\x00{exp}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def make_csrf(secret: str, owner_id: str, ext_id: str, action_id: str, *, now: float | None = None) -> str:
    exp = int((time.time() if now is None else now) + CSRF_TTL_SECONDS)
    return f"{exp}.{_csrf_sig(secret, owner_id, ext_id, action_id, exp)}"


def verify_csrf(
    secret: str, token: str, owner_id: str, ext_id: str, action_id: str, *, now: float | None = None
) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(sig, _csrf_sig(secret, owner_id, ext_id, action_id, exp))


__all__ = [
    "ActionRejected",
    "ExtView",
    "FieldSpec",
    "active_extensions",
    "build_plans",
    "evaluate",
    "make_csrf",
    "parse_form",
    "run_action",
    "run_on_pod_ready",
    "save_form",
    "verify_csrf",
    "wipe_secrets",
]
