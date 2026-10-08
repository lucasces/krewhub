"""Tabela `dev_extensions` -- persistência de config/estado por extensão."""

from __future__ import annotations

import json

from app import store


def test_roundtrip_and_partial_updates(tmp_path):
    with store.connect(str(tmp_path / "s.db")) as conn:
        assert store.get_extension(conn, "o", "demo") is None
        store.upsert_extension(conn, "o", "demo", enabled=True, config={"region": "us-east-1"})
        store.upsert_extension(conn, "o", "demo", runtime_state={"x": 1})
        ext = store.get_extension(conn, "o", "demo")
        assert ext["enabled"] is True
        assert ext["config"] == {"region": "us-east-1"}
        assert ext["runtime_state"] == {"x": 1}
        assert ext["secrets"] == {}


def test_other_owner_and_extension_are_isolated(tmp_path):
    with store.connect(str(tmp_path / "s.db")) as conn:
        store.upsert_extension(conn, "a", "demo", enabled=True)
        store.upsert_extension(conn, "b", "demo", enabled=False)
        store.upsert_extension(conn, "a", "other", enabled=True)
        assert set(store.list_extensions(conn, "a")) == {"demo", "other"}
        assert set(store.list_extensions(conn, "b")) == {"demo"}
        assert store.list_extensions(conn, "b")["demo"]["enabled"] is False


def test_secret_markers_never_hold_values(tmp_path):
    with store.connect(str(tmp_path / "s.db")) as conn:
        store.upsert_extension(
            conn, "o", "demo", config={"url": "u"}, secrets={"token": store.secret_marker()}
        )
        raw = conn.execute("SELECT config_json FROM dev_extensions").fetchone()[0]
        assert json.loads(raw)["__secrets__"]["token"]["set"] is True
        ext = store.get_extension(conn, "o", "demo")
        assert ext["config"] == {"url": "u"}
        assert ext["secrets"]["token"]["set"] is True


def test_config_update_keeps_secret_markers(tmp_path):
    with store.connect(str(tmp_path / "s.db")) as conn:
        store.upsert_extension(conn, "o", "demo", secrets={"token": store.secret_marker()})
        store.upsert_extension(conn, "o", "demo", config={"url": "u2"})
        assert "token" in store.get_extension(conn, "o", "demo")["secrets"]


def test_reset_runtime_drops_state_and_selected_markers(tmp_path):
    with store.connect(str(tmp_path / "s.db")) as conn:
        store.upsert_extension(
            conn,
            "o",
            "demo",
            enabled=True,
            config={"url": "u"},
            secrets={"a": store.secret_marker(), "b": store.secret_marker()},
            runtime_state={"s": 1},
        )
        store.reset_extension_runtime(conn, "o", "demo", drop_secret_keys={"a"})
        ext = store.get_extension(conn, "o", "demo")
        assert ext["runtime_state"] == {}
        assert set(ext["secrets"]) == {"b"}
        assert ext["enabled"] is True and ext["config"] == {"url": "u"}
        store.reset_extension_runtime(conn, "o", "demo", drop_secret_keys={"b"})
        raw = conn.execute("SELECT config_json FROM dev_extensions").fetchone()[0]
        assert "__secrets__" not in json.loads(raw)
        store.reset_extension_runtime(conn, "o", "ghost")
