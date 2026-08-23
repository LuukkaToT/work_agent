"""
checkpoint.py / ledger.py 的后端选择逻辑：POSTGRES_DSN 未配置时必须落回本地 SQLite。

不需要真实 Postgres：只验证「DSN 为空串 → 用 SqliteSaver/RunLedger」这条路径，
不管本机 .env 里实际有没有配 DSN，都应该始终成立（避免这条回退路径被悄悄改坏）。
"""

from __future__ import annotations

from dataclasses import replace

from langgraph.checkpoint.sqlite import SqliteSaver

import work_agent.core.checkpoint as checkpoint_mod
import work_agent.core.ledger as ledger_mod
from work_agent.core.config import get_settings


def _settings_without_dsn(tmp_path, **overrides):
    base = get_settings()
    return replace(
        base,
        postgres_dsn="",
        checkpoint_path=tmp_path / "cp.sqlite",
        workspace_dir=tmp_path,
        **overrides,
    )


def test_get_checkpointer_falls_back_to_sqlite_without_dsn(monkeypatch, tmp_path):
    monkeypatch.setattr(
        checkpoint_mod, "get_settings", lambda: _settings_without_dsn(tmp_path)
    )
    checkpoint_mod.get_checkpointer.cache_clear()
    try:
        saver = checkpoint_mod.get_checkpointer()
        assert isinstance(saver, SqliteSaver)
    finally:
        checkpoint_mod.get_checkpointer.cache_clear()


def test_get_ledger_falls_back_to_sqlite_without_dsn(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ledger_mod, "get_settings", lambda: _settings_without_dsn(tmp_path)
    )
    ledger_mod.get_ledger.cache_clear()
    try:
        ledger = ledger_mod.get_ledger()
        assert isinstance(ledger, ledger_mod.RunLedger)
    finally:
        ledger_mod.get_ledger.cache_clear()
