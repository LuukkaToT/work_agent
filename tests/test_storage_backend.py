"""未配 DSN 时连接池 / checkpointer / ledger 必须显式报错，不再回落到 SQLite。"""

from __future__ import annotations

from dataclasses import replace

import pytest

import work_agent.core.checkpoint as checkpoint_mod
import work_agent.core.db as db_mod
import work_agent.core.ledger as ledger_mod
import work_agent.core.user_config as user_config_mod
from work_agent.core.config import get_settings
from work_agent.core.db_init import init_database


def _settings_without_dsn():
    return replace(get_settings(), postgres_dsn="", postgres_test_dsn="")


def _patch_empty_dsn(monkeypatch) -> None:
    empty = _settings_without_dsn()
    monkeypatch.setattr(db_mod, "get_settings", lambda: empty)
    db_mod.reset_pool_cache()
    checkpoint_mod.get_checkpointer.cache_clear()
    ledger_mod.get_ledger.cache_clear()
    user_config_mod.get_user_config_store.cache_clear()


def test_get_pool_raises_without_dsn(monkeypatch):
    _patch_empty_dsn(monkeypatch)
    with pytest.raises(RuntimeError, match="POSTGRES_DSN"):
        db_mod.get_pool()


def test_get_checkpointer_raises_without_dsn(monkeypatch):
    _patch_empty_dsn(monkeypatch)
    with pytest.raises(RuntimeError, match="POSTGRES_DSN"):
        checkpoint_mod.get_checkpointer()


def test_get_ledger_is_postgres_type_but_pool_raises_on_use(monkeypatch):
    """get_ledger() 不再选 SQLite；真正碰库时由 get_pool 报错。"""
    _patch_empty_dsn(monkeypatch)
    ledger = ledger_mod.get_ledger()
    assert isinstance(ledger, ledger_mod.PostgresLedger)
    with pytest.raises(RuntimeError, match="POSTGRES_DSN"):
        ledger.list_recent()


def test_init_database_raises_without_dsn():
    with pytest.raises(RuntimeError, match="DSN"):
        init_database("")
