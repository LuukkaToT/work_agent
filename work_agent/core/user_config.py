"""
按用户持久化个人偏好（``debug_mode``、``version_space``）。

只走 Postgres。表由 ``python -m work_agent init-db`` 创建，运行时不建表。

字段设计：``config`` 列存 JSON blob
（``{"debug_mode": true, "version_space": "27B"}``），
不是一列一个字段——和 ``ledger.py`` 里 ``case_names`` 的存法一致，
以后加新偏好不用改表结构。``update`` 是合并语义：只覆盖传入的键，
其它已有键保留。

读接口返回 ``None`` 表示用户从未设置过该字段，交给调用方套系统默认值
或走 HITL；不要把「未设置」和 ``False`` / 空串混为一谈。偏好由前端
``GET/PATCH /users/me/config`` 读写，不进图状态、不走聊天意图。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Protocol

from work_agent.core.db import get_pool

# 与流水线 ``version`` 同一套枚举；CI 用例目录不再保存版本。
VERSION_SPACES = frozenset({"27B", "27A", "26B", "26A"})


def _now() -> str:
    """UTC ISO 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


class UserConfigStore(Protocol):
    """个人配置后端协议。"""

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


class PostgresUserConfigStore:
    """Postgres 个人配置。表须已由 init-db 建好。"""

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
    返回进程内共享的个人配置实例。

    返回:
        ``PostgresUserConfigStore``。未配 DSN 时第一次查库由 ``get_pool()`` 报错。
    """
    return PostgresUserConfigStore()


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


def _normalize_version_space(value: object) -> str | None:
    """空 / 非法版本视为未设置；合法值规范成大写。"""
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    if text not in VERSION_SPACES:
        return None
    return text


def get_version_space(user_id: str) -> str | None:
    """
    读某用户的默认版本空间。

    参数:
        user_id: 工号。

    返回:
        ``27B`` / ``27A`` / ``26B`` / ``26A``；从未设置或库里是非法值时返回 ``None``。
    """
    return _normalize_version_space(
        get_user_config_store().get(user_id).get("version_space")
    )


def set_version_space(user_id: str, value: str | None) -> None:
    """
    写某用户的默认版本空间；``None`` 表示清空（之后补参会落到 HITL）。

    参数:
        user_id: 工号。
        value: 版本枚举，或 ``None`` 清空。大小写不敏感。

    异常:
        ValueError: 非空但不是 ``VERSION_SPACES`` 之一。
    """
    if value is None or str(value).strip() == "":
        get_user_config_store().update(user_id, version_space=None)
        return
    text = str(value).strip().upper()
    if text not in VERSION_SPACES:
        allowed = ", ".join(sorted(VERSION_SPACES))
        raise ValueError(f"version_space 必须是 {allowed} 之一，收到 {value!r}")
    get_user_config_store().update(user_id, version_space=text)
