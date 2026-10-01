"""Renderização server-side (HTML puro, sem JS) das seções de extensões.

Único lugar onde dados de extensão viram HTML -- TODO texto vindo da
extensão ou do dev passa por `html.escape`; links só saem com esquema
http(s). Extensões devolvem dados (`Card`, `FieldSpec`), nunca markup."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence
from urllib.parse import quote, urlparse

from app.extensions.base import Card, Extension, FieldSpec

_STATE_LABEL = {
    "inactive": "desativada",
    "pending": "aguardando",
    "needs_action": "precisa de ação",
    "ready": "pronta",
    "degraded": "degradada",
    "error": "erro",
}
_STATE_COLOR = {
    "inactive": "#777",
    "pending": "#a60",
    "needs_action": "#05a",
    "ready": "#080",
    "degraded": "#a60",
    "error": "#b00",
}

FIELD_PREFIX = "ext"


def field_name(ext_id: str, key: str) -> str:
    return f"{FIELD_PREFIX}.{ext_id}.{key}"


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _safe_url(url: str) -> str | None:
    parsed = urlparse(url)
    return url if parsed.scheme in ("http", "https") and parsed.netloc else None


@dataclass(frozen=True)
class ActionButton:
    id: str
    label: str
    enabled: bool
    description: str = ""
    params: tuple[FieldSpec, ...] = ()


@dataclass(frozen=True)
class CardView:
    ext_id: str
    name: str
    state: str
    card: Card
    actions: tuple[ActionButton, ...] = ()
    last_action: str = ""


# ---------------------------------------------------------------------------
# Seção de configuração (dentro do form do lobby)
# ---------------------------------------------------------------------------


def _render_field(ext_id: str, f: FieldSpec, config: Mapping, secrets: Mapping) -> str:
    name = _e(field_name(ext_id, f.key))
    label = _e(f.label or f.key)
    hint = f'<br><small>{_e(f.help)}</small>' if f.help else ""
    if f.kind == "bool":
        checked = " checked" if str(config.get(f.key, f.default)).lower() in ("1", "true", "on") else ""
        return f'<p><label><input type="checkbox" name="{name}"{checked}> {label}</label>{hint}</p>'
    if f.kind == "select":
        current = str(config.get(f.key, f.default))
        opts = "".join(
            f'<option value="{_e(o)}"{" selected" if o == current else ""}>{_e(o)}</option>'
            for o in f.options
        )
        return f"<p><label>{label}:<br><select name=\"{name}\">{opts}</select></label>{hint}</p>"
    if f.kind == "secret":
        is_set = bool((secrets.get(f.key) or {}).get("set"))
        placeholder = "já definido -- deixe vazio pra manter" if is_set else ""
        return (
            f'<p><label>{label}:<br><input type="password" name="{name}" size="40" '
            f'autocomplete="new-password" value="" placeholder="{_e(placeholder)}"></label>{hint}</p>'
        )
    if f.kind == "generated":
        return f"<p><small>{label}: gerado automaticamente.</small></p>"
    value = _e(config.get(f.key, f.default))
    return (
        f'<p><label>{label}:<br><input type="text" name="{name}" size="50" value="{value}"></label>{hint}</p>'
    )


def render_config_section(
    entries: Sequence[tuple[Extension, Mapping | None, Sequence[str]]],
) -> str:
    """`entries`: `(extensão, linha do store | None, erros de validação)`.
    Vazio quando nenhuma extensão está habilitada pelo admin."""
    if not entries:
        return ""
    parts = ["<h2>Extensões</h2>"]
    for ext, row, errors in entries:
        row = row or {}
        config = row.get("config", {})
        secrets = row.get("secrets", {})
        enabled = " checked" if row.get("enabled") else ""
        err = "".join(f'<p style="color:#b00;margin:.2rem 0">{_e(m)}</p>' for m in errors)
        body = "".join(_render_field(ext.id, f, config, secrets) for f in ext.fields)
        desc = f"<p><small>{_e(ext.description)}</small></p>" if ext.description else ""
        parts.append(
            f"<fieldset><legend><label>"
            f'<input type="checkbox" name="{_e(field_name(ext.id, "enabled"))}"{enabled}> '
            f"{_e(ext.name or ext.id)}</label></legend>{desc}{err}{body}</fieldset>"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Cartões
# ---------------------------------------------------------------------------


def render_card(owner_id: str, view: CardView, csrf: Callable[[str, str], str]) -> str:
    card = view.card
    color = _STATE_COLOR.get(view.state, "#777")
    label = _STATE_LABEL.get(view.state, view.state)
    parts = [
        '<section style="border:1px solid #ccc;border-radius:6px;padding:.8rem 1rem;margin:.8rem 0">',
        f'<h3 style="margin:.1rem 0">{_e(card.title or view.name)} '
        f'<small style="color:{color}">[{_e(label)}]</small></h3>',
    ]
    if card.summary:
        parts.append(f"<p>{_e(card.summary)}</p>")
    if card.code:
        parts.append(
            f'<p>Código: <code style="font-size:1.3rem;letter-spacing:.1rem">{_e(card.code)}</code></p>'
        )
    for link in card.links:
        url = _safe_url(link.url)
        if url:
            parts.append(
                f'<p><a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{_e(link.label)}</a></p>'
            )
    if card.rows:
        rows = "".join(f"<tr><th align=\"left\">{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in card.rows)
        parts.append(f"<table>{rows}</table>")
    for msg in card.messages:
        parts.append(f"<p><small>{_e(msg)}</small></p>")
    if view.last_action:
        parts.append(f"<p><small><em>{_e(view.last_action)}</em></small></p>")
    for a in view.actions:
        parts.append(_render_action(owner_id, view.ext_id, a, csrf))
    parts.append("</section>")
    return "\n".join(parts)


def _render_action(owner_id: str, ext_id: str, a: ActionButton, csrf: Callable[[str, str], str]) -> str:
    action_url = (
        f"/devs/{quote(owner_id, safe='@')}/extensions/{quote(ext_id, safe='')}/actions/{quote(a.id, safe='')}"
    )
    params = "".join(
        f'<label>{_e(p.label or p.key)}: <input type="text" name="{_e(p.key)}" value="{_e(p.default)}"></label> '
        for p in a.params
    )
    disabled = "" if a.enabled else " disabled"
    title = f' title="{_e(a.description)}"' if a.description else ""
    return (
        f'<form method="post" action="{_e(action_url)}" style="display:inline-block;margin:.2rem .4rem .2rem 0">'
        f'<input type="hidden" name="csrf" value="{_e(csrf(ext_id, a.id))}">{params}'
        f'<button type="submit"{disabled}{title}>{_e(a.label)}</button></form>'
    )


def render_cards_document(
    owner_id: str,
    views: Sequence[CardView],
    csrf: Callable[[str, str], str],
    *,
    refresh_seconds: int | None = None,
) -> str:
    """Documento completo (servido sozinho e embutido via `<iframe>` no
    lobby). `refresh_seconds` liga `<meta http-equiv="refresh">` -- único
    mecanismo de atualização (sem JS)."""
    refresh = (
        f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">' if refresh_seconds else ""
    )
    body = (
        "\n".join(render_card(owner_id, v, csrf) for v in views)
        if views
        else "<p><small>Nenhuma extensão ativa.</small></p>"
    )
    return (
        '<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">'
        f"{refresh}<title>KrewHub -- extensões</title></head>"
        f'<body style="font-family:sans-serif;margin:.5rem">{body}</body></html>'
    )


def render_cards_iframe(owner_id: str) -> str:
    src = f"/devs/{quote(owner_id, safe='@')}/extensions/cards"
    return (
        f'<h2>Extensões</h2><iframe src="{_e(src)}" title="Extensões" '
        'style="width:100%;height:24rem;border:0"></iframe>'
    )
