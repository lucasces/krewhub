"""Descoberta de extensões via entry points (`krewhub.extensions`).

"Instalada" e "habilitada" são coisas diferentes: instalar é `pip install`
do pacote da extensão na imagem; habilitar é decisão do admin em
`KREWHUB_EXTENSIONS_ENABLED` (lista de ids separados por vírgula).

`discover()` importa e instancia TODA extensão instalada, habilitada ou
não (precisa do `id` e da definição pra validar o entry point), então o
código de um pacote instalado roda no processo do KrewHub. O que a
habilitação controla é o efeito: só uma extensão instalada E habilitada
entra em `enabled_extensions()`, e só ela contribui com o Pod, aparece no
lobby e recebe ações. Por isso "instalar" já é uma decisão de confiança
do admin, não só "habilitar"."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any, Callable, Iterable

from app.extensions.base import API_VERSION, Extension

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "krewhub.extensions"

# Plugáveis em teste: `() -> iterable de EntryPoint`.
_entry_points: Callable[[], Iterable[Any]] = lambda: entry_points(group=ENTRY_POINT_GROUP)

_installed: dict[str, Extension] | None = None
load_errors: dict[str, str] = {}


def _major(version: str) -> str:
    return str(version).split(".", 1)[0]


def discover(*, refresh: bool = False) -> dict[str, Extension]:
    """Carrega (uma vez, com cache) todas as extensões instaladas.
    Plugin quebrado (import falha, classe inválida, api_version de outro
    major, nome do entry point diferente do `id`) é logado, registrado em
    `load_errors` e pulado -- nunca derruba o KrewHub."""
    global _installed
    if _installed is not None and not refresh:
        return _installed

    found: dict[str, Extension] = {}
    load_errors.clear()
    for ep in _entry_points():
        try:
            cls = ep.load()
            if not (isinstance(cls, type) and issubclass(cls, Extension)):
                raise TypeError("não é subclasse de app.extensions.base.Extension")
            if _major(cls.api_version) != _major(API_VERSION):
                raise ValueError(
                    f"api_version {cls.api_version!r} incompatível com a do KrewHub ({API_VERSION!r})"
                )
            ext = cls()
            ext.check_definition()
            if ep.name != ext.id:
                raise ValueError(f"nome do entry point {ep.name!r} difere do id {ext.id!r}")
            if ext.id in found:
                raise ValueError(f"id {ext.id!r} duplicado")
            found[ext.id] = ext
        except Exception as exc:  # noqa: BLE001 -- plugin de terceiro, isola qualquer falha
            logger.error("extensão %r ignorada: %s", ep.name, exc)
            load_errors[ep.name] = str(exc)
    _installed = found
    return found


def parse_enabled(raw: str) -> list[str]:
    seen: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part and part not in seen:
            seen.append(part)
    return seen


def enabled_extensions(settings) -> dict[str, Extension]:
    """Extensões instaladas E habilitadas pelo admin, na ordem de
    `KREWHUB_EXTENSIONS_ENABLED`. Id habilitado mas não instalado vira
    warning (típico: pacote faltando na imagem)."""
    installed = discover()
    result: dict[str, Extension] = {}
    for ext_id in parse_enabled(getattr(settings, "extensions_enabled", "")):
        ext = installed.get(ext_id)
        if ext is None:
            logger.warning(
                "extensão %r está em KREWHUB_EXTENSIONS_ENABLED mas não está instalada", ext_id
            )
            continue
        result[ext_id] = ext
    return result


def reset_for_tests(entry_points_fn: Callable[[], Iterable[Any]] | None = None) -> None:
    global _installed, _entry_points
    _installed = None
    load_errors.clear()
    _entry_points = entry_points_fn or (lambda: entry_points(group=ENTRY_POINT_GROUP))
