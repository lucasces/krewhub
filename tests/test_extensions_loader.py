"""Loader de entry points (`app.extensions`) e contrato de `base.py`."""

from __future__ import annotations

import logging

import pytest

from app import extensions
from app.config import Settings
from app.extensions import base


class _Good(base.Extension):
    id = "good"
    name = "Good"


class _OtherMajor(base.Extension):
    id = "future"
    api_version = "2.0"


class _BadId(base.Extension):
    id = "Bad_ID"


class _Boom:
    pass


class _EP:
    def __init__(self, name, target):
        self.name = name
        self._target = target

    def load(self):
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


@pytest.fixture(autouse=True)
def _restore_loader():
    yield
    extensions.reset_for_tests()


def _install(*eps):
    extensions.reset_for_tests(lambda: list(eps))


def test_discover_loads_valid_extension():
    _install(_EP("good", _Good))
    assert set(extensions.discover()) == {"good"}
    assert extensions.load_errors == {}


def test_discover_skips_broken_plugins_and_records_errors(caplog):
    _install(
        _EP("good", _Good),
        _EP("future", _OtherMajor),
        _EP("bad", _BadId),
        _EP("boom", _Boom),
        _EP("crash", ImportError("sem módulo")),
        _EP("mismatch", _Good),
    )
    with caplog.at_level(logging.ERROR):
        found = extensions.discover()
    assert set(found) == {"good"}
    assert set(extensions.load_errors) == {"future", "bad", "boom", "crash", "mismatch"}
    assert "incompatível" in extensions.load_errors["future"]
    assert "difere do id" in extensions.load_errors["mismatch"]


def test_discover_is_cached_until_refresh():
    calls = []

    def eps():
        calls.append(1)
        return [_EP("good", _Good)]

    extensions.reset_for_tests(eps)
    extensions.discover()
    extensions.discover()
    assert len(calls) == 1
    extensions.discover(refresh=True)
    assert len(calls) == 2


def _settings(settings: Settings, enabled: str) -> Settings:
    import dataclasses

    return dataclasses.replace(settings, extensions_enabled=enabled)


def test_installed_but_not_enabled_is_unusable(settings):
    _install(_EP("good", _Good))
    assert extensions.enabled_extensions(_settings(settings, "")) == {}


def test_enabled_but_not_installed_warns(settings, caplog):
    _install(_EP("good", _Good))
    with caplog.at_level(logging.WARNING):
        result = extensions.enabled_extensions(_settings(settings, "good, ghost,good"))
    assert list(result) == ["good"]
    assert "ghost" in caplog.text


def test_parse_enabled_dedups_and_trims():
    assert extensions.parse_enabled(" a, b ,,a") == ["a", "b"]


def test_field_spec_validation():
    with pytest.raises(ValueError):
        base.FieldSpec(key="Bad Key")
    with pytest.raises(ValueError):
        base.FieldSpec(key="x", kind="select")
    with pytest.raises(ValueError):
        base.FieldSpec(key="x", kind="secret", default="oops")


def test_default_validate_checks_required_select_and_pattern():
    class E(base.Extension):
        id = "e"
        fields = (
            base.FieldSpec("url", "URL", required=True, pattern=r"https://\S+"),
            base.FieldSpec("region", "Região", kind="select", options=("a", "b")),
            base.FieldSpec("tok", kind="secret", required=True),
        )

    e = E()
    assert e.validate({"url": "", "region": "z"}) == ["URL: obrigatório", "Região: valor inválido"]
    assert e.validate({"url": "http://x"}) == ["URL: formato inválido"]
    assert e.validate({"url": "https://x", "region": "a"}) == []


def test_derive_state():
    d = base.derive_state
    ok = {"pod.ready": True, "a": True}
    assert d({"pod.ready": False, "a": True}, ready_when=["a"]) == "pending"
    assert d(ok, ready_when=["a"]) == "ready"
    assert d({"pod.ready": True, "a": False}, ready_when=["a"]) == "needs_action"
    assert d({**ok, "bad": True}, ready_when=["a"], degraded_when=["bad"]) == "degraded"


def test_check_definition_rejects_bad_id_and_duplicates():
    with pytest.raises(ValueError):
        _BadId().check_definition()

    class Dup(base.Extension):
        id = "dup"
        fields = (base.FieldSpec("a"), base.FieldSpec("a"))

    with pytest.raises(ValueError):
        Dup().check_definition()
