"""会话列表：从 checkpointer SQLite 读 thread，供 CLI /session 使用。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from work_agent.core.checkpoint import get_checkpointer, make_thread_config


@dataclass(frozen=True)
class SessionInfo:
    thread_id: str
    updated_at: str  # checkpoint_id（时间可排序）或可读时间
    preview: str
    pending: bool = False


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "".join(parts).strip()
    return str(content or "").strip()


def _preview_from_values(values: dict[str, Any], *, max_len: int = 48) -> str:
    reply = (values.get("reply") or "").strip()
    if reply:
        text = reply
    else:
        msgs = values.get("messages") or []
        text = ""
        for msg in reversed(msgs):
            content = _message_text(getattr(msg, "content", ""))
            if content:
                text = content
                break
        if not text:
            text = (values.get("user_input") or "").strip() or "(空会话)"
    text = " ".join(text.split())
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def list_sessions(limit: int = 20) -> list[SessionInfo]:
    """最近更新的 thread 列表（checkpoint_ns 为空的主图）。"""
    saver = get_checkpointer()
    conn = saver.conn
    rows = conn.execute(
        """
        SELECT thread_id, MAX(checkpoint_id) AS last_id
        FROM checkpoints
        WHERE checkpoint_ns = ''
        GROUP BY thread_id
        ORDER BY last_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    out: list[SessionInfo] = []
    for thread_id, last_id in rows:
        preview = "(无法读取)"
        pending = False
        try:
            tup = saver.get_tuple(make_thread_config(thread_id))
            if tup is not None:
                values = tup.checkpoint.get("channel_values") or {}
                preview = _preview_from_values(values)
                # pending writes / 未完成节点：靠 channel 不够稳，留给 runtime 查图
        except Exception:  # noqa: BLE001
            pass
        out.append(
            SessionInfo(
                thread_id=thread_id,
                updated_at=str(last_id or ""),
                preview=preview,
                pending=pending,
            )
        )
    return out


def session_exists(thread_id: str) -> bool:
    if not thread_id:
        return False
    saver = get_checkpointer()
    row = saver.conn.execute(
        """
        SELECT 1 FROM checkpoints
        WHERE thread_id = ? AND checkpoint_ns = ''
        LIMIT 1
        """,
        (thread_id,),
    ).fetchone()
    return row is not None


def resolve_session_pick(
    choice: str,
    sessions: list[SessionInfo],
) -> str | None:
    """
    解析用户选择：
      空 / 非法 → None
      纯数字序号（1-based）→ 对应 thread_id
      其它 → 当作 thread_id 原文
    """
    text = (choice or "").strip()
    if not text:
        return None
    if text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(sessions):
            return sessions[idx - 1].thread_id
        return None
    return text
