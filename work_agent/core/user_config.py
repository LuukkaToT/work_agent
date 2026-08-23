"""
按用户持久化个人偏好（目前只有 ``debug_mode``）。

后端选择与 checkpointer / ledger 一致：``POSTGRES_DSN`` 配了用
``PostgresUserConfigStore``（走 ``core/db.py`` 连接池），没配退回本地
``SqliteUserConfigStore``（``workspace/user_config.db``）。

字段设计：``config`` 列存 JSON blob（``{"debug_mode": true, ...}``），
不是一列一个字段——和 ``ledger.py`` 里 ``case_names`` 的存法一致，
以后加新偏好不用改表结构。``update`` 是合并语义：只覆盖传入的键，
其它已有键保留。

``get_debug_mode`` 返回 ``None`` 表示用户从未设置过，交给调用方套系统
默认值；不要把「未设置」和 ``False`` 混为一谈。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from work_agent.core.config import get_settings
from work_agent.core.db import get_pool


def _now() -> str:
    """UTC ISO 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


class UserConfigStore(Protocol):
    """个人配置后端协议：SQLite / Postgres 都实现这套方法。"""

    def get(self, user_id: str) -> dict[str, Any]:
        """
        取某用户的完整配置 dict。

        参数:
            user_id: 工号。

        返回:
            配置 dict；用户从未写过任何配置时返回空 dict ``{}``（不是 None）。
        """
        ...

    def update(self, user_id: str, **fields: Any) -> dict[str, Any]:
        """
        合并写入若干字段，返回更新后的完整配置。

        参数:
            user_id: 工号。
            **fields: 要覆盖的键值；未传入的已有键保持不变。

        返回:
            合并后的完整配置 dict。
        """
        ...


class SqliteUserConfigStore:
    """本地 SQLite 个人配置：``workspace/user_config.db``。"""

    def __init__(self, db_path: Path) -> None:
        """
        参数:
            db_path: 库文件路径；父目录不存在会自动创建。
        """
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_config (
                    user_id TEXT PRIMARY KEY,
                    config TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def get(self, user_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT config FROM user_config WHERE user_id=?", (user_id,)
            ).fetchone()
        if not row:
            return {}
        return json.loads(row["config"] or "{}")

    def update(self, user_id: str, **fields: Any) -> dict[str, Any]:
        current = self.get(user_id)
        current.update(fields)
        now = _now()
        blob = json.dumps(current, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_config (user_id, config, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    config=excluded.config,
                    updated_at=excluded.updated_at
                """,
                (user_id, blob, now),
            )
            conn.commit()
        return current


class PostgresUserConfigStore:
    """Postgres 个人配置：上线用，方法签名与 ``SqliteUserConfigStore`` 对齐。"""

    def __init__(self) -> None:
        self._ensure_table()

    def _ensure_table(self) -> None:
        with get_pool().connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_config (
                    user_id TEXT PRIMARY KEY,
                    config TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                )
                """
            )

    def get(self, user_id: str) -> dict[str, Any]:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT config FROM user_config WHERE user_id=%s", (user_id,)
            ).fetchone()
        if not row:
            return {}
        return json.loads(row["config"] or "{}")

    def update(self, user_id: str, **fields: Any) -> dict[str, Any]:
        current = self.get(user_id)
        current.update(fields)
        now = _now()
        blob = json.dumps(current, ensure_ascii=False)
        with get_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO user_config (user_id, config, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    config=excluded.config,
                    updated_at=excluded.updated_at
                """,
                (user_id, blob, now),
            )
        return current


@lru_cache(maxsize=1)
def get_user_config_store() -> UserConfigStore:
    """
    返回进程内共享的个人配置实例；按 ``POSTGRES_DSN`` 是否配置自动选后端。

    返回:
        ``PostgresUserConfigStore``（配了 DSN）或 ``SqliteUserConfigStore``（本地 SQLite）。
    """
    if get_settings().postgres_dsn:
        return PostgresUserConfigStore()
    return SqliteUserConfigStore(get_settings().workspace_dir / "user_config.db")


def get_debug_mode(user_id: str) -> bool | None:
    """
    读某用户的 ``debug_mode`` 偏好。

    参数:
        user_id: 工号。

    返回:
        ``True`` / ``False``；用户从未设置过该字段时返回 ``None``
        （让调用方套系统默认值，不要把「未设置」当成 ``False``）。
    """
    value = get_user_config_store().get(user_id).get("debug_mode")
    if value is None:
        return None
    return bool(value)


def set_debug_mode(user_id: str, value: bool) -> None:
    """
    写某用户的 ``debug_mode`` 偏好。

    参数:
        user_id: 工号。
        value: 是否开启调测模式。
    """
    get_user_config_store().update(user_id, debug_mode=bool(value))
