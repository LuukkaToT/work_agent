"""
core/user_config.py：SQLite 后端 CRUD + 无 DSN 时落回 SQLite。

Postgres 真库测试在 test_postgres_integration.py（模块级 skipif）。
"""

from __future__ import annotations

from dataclasses import replace

import work_agent.core.user_config as user_config_mod
from work_agent.core.config import get_settings
from work_agent.core.user_config import (
    SqliteUserConfigStore,
    get_debug_mode,
    set_debug_mode,
)


def test_sqlite_get_returns_empty_dict_for_unknown_user(tmp_path):
    store = SqliteUserConfigStore(tmp_path / "user_config.db")
    assert store.get("z00001") == {}


def test_sqlite_update_is_merge_not_replace(tmp_path):
    store = SqliteUserConfigStore(tmp_path / "user_config.db")
    store.update("z00001", debug_mode=True)
    store.update("z00001", theme="dark")

    cfg = store.get("z00001")
    assert cfg == {"debug_mode": True, "theme": "dark"}


def test_sqlite_update_overwrites_same_key(tmp_path):
    store = SqliteUserConfigStore(tmp_path / "user_config.db")
    store.update("z00001", debug_mode=True)
    store.update("z00001", debug_mode=False)
    assert store.get("z00001")["debug_mode"] is False


def test_sqlite_users_are_isolated(tmp_path):
    store = SqliteUserConfigStore(tmp_path / "user_config.db")
    store.update("alice", debug_mode=True)
    store.update("bob", debug_mode=False)

    assert store.get("alice") == {"debug_mode": True}
    assert store.get("bob") == {"debug_mode": False}


def test_get_debug_mode_returns_none_when_unset(monkeypatch, tmp_path):
    store = SqliteUserConfigStore(tmp_path / "user_config.db")
    monkeypatch.setattr(user_config_mod, "get_user_config_store", lambda: store)

    assert get_debug_mode("z00001") is None


def test_get_set_debug_mode_roundtrip(monkeypatch, tmp_path):
    store = SqliteUserConfigStore(tmp_path / "user_config.db")
    monkeypatch.setattr(user_config_mod, "get_user_config_store", lambda: store)

    set_debug_mode("z00001", True)
    assert get_debug_mode("z00001") is True

    set_debug_mode("z00001", False)
    assert get_debug_mode("z00001") is False


def test_get_user_config_store_falls_back_to_sqlite_without_dsn(monkeypatch, tmp_path):
    base = get_settings()
    monkeypatch.setattr(
        user_config_mod,
        "get_settings",
        lambda: replace(base, postgres_dsn="", workspace_dir=tmp_path),
    )
    user_config_mod.get_user_config_store.cache_clear()
    try:
        store = user_config_mod.get_user_config_store()
        assert isinstance(store, SqliteUserConfigStore)
    finally:
        user_config_mod.get_user_config_store.cache_clear()
