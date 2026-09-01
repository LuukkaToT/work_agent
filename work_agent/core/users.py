"""用户目录；权限和角色在后续阶段接入。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from work_agent.core.db import get_pool

_USER_ID_RE = re.compile(r"^[A-Za-z][0-9]{8}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass(frozen=True)
class UserRecord:
    id: str
    cn_name: str
    email: str


def _normalize_user(user_id: str, cn_name: str, email: str) -> UserRecord:
    normalized_id = (user_id or "").strip().lower()
    normalized_name = (cn_name or "").strip()
    normalized_email = (email or "").strip().lower()
    if not _USER_ID_RE.fullmatch(normalized_id):
        raise ValueError("工号必须是一个字母加 8 位数字")
    if not normalized_name:
        raise ValueError("cn_name 不能为空")
    if not _EMAIL_RE.fullmatch(normalized_email):
        raise ValueError("邮箱格式不合法")
    return UserRecord(normalized_id, normalized_name, normalized_email)


def get_user(user_id: str) -> UserRecord | None:
    normalized_id = (user_id or "").strip().lower()
    if not normalized_id:
        return None
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT id, cn_name, email FROM users WHERE id=%s", (normalized_id,)
        ).fetchone()
    return UserRecord(**dict(row)) if row else None


def upsert_user(user_id: str, cn_name: str, email: str) -> UserRecord:
    record = _normalize_user(user_id, cn_name, email)
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO users (id, cn_name, email)
            VALUES (%s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                cn_name=excluded.cn_name,
                email=excluded.email
            """,
            (record.id, record.cn_name, record.email),
        )
    return record
