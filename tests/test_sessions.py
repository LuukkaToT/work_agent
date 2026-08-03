"""会话列表与 /session 选择解析。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from work_agent.core.sessions import (
    SessionInfo,
    list_sessions,
    resolve_session_pick,
    session_exists,
)


def test_resolve_session_pick_by_index():
    sessions = [
        SessionInfo("a", "1", "x"),
        SessionInfo("b", "2", "y"),
    ]
    assert resolve_session_pick("1", sessions) == "a"
    assert resolve_session_pick("2", sessions) == "b"
    assert resolve_session_pick("3", sessions) is None
    assert resolve_session_pick("0", sessions) is None


def test_resolve_session_pick_by_id():
    sessions = [SessionInfo("cli-abc", "1", "hi")]
    assert resolve_session_pick("cli-abc", sessions) == "cli-abc"
    assert resolve_session_pick("other-id", sessions) == "other-id"
    assert resolve_session_pick("  ", sessions) is None
    assert resolve_session_pick("", sessions) is None


def _patch_checkpointer(monkeypatch, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()

    monkeypatch.setattr(
        "work_agent.core.sessions.get_checkpointer", lambda: saver
    )
    return saver


def test_list_sessions_and_exists(tmp_path, monkeypatch):
    saver = _patch_checkpointer(monkeypatch, tmp_path / "cp.sqlite")
    # 直接写两行不同 thread（无需完整 checkpoint blob 也能 list / exists）
    for tid, cid in (("thread-old", "1"), ("thread-new", "2")):
        saver.conn.execute(
            """
            INSERT INTO checkpoints
            (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata)
            VALUES (?, '', ?, NULL, NULL, ?, ?)
            """,
            (tid, cid, b"{}", b"{}"),
        )
    saver.conn.commit()

    sessions = list_sessions(limit=10)
    assert [s.thread_id for s in sessions] == ["thread-new", "thread-old"]
    assert session_exists("thread-new")
    assert not session_exists("nope")
    assert not session_exists("")
