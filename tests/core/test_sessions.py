"""会话列表与 /session 选择解析。"""

from __future__ import annotations

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


def test_list_sessions_and_exists(monkeypatch):
    class _Saver:
        def get_tuple(self, config):
            return None

    # 补丁目标必须与 sessions.py 从 checkpoint 导入的 query_recent_threads 同名
    monkeypatch.setattr(
        "work_agent.core.sessions.get_checkpointer", lambda: _Saver()
    )
    monkeypatch.setattr(
        "work_agent.core.sessions.query_recent_threads",
        lambda saver, limit: [("thread-new", "2"), ("thread-old", "1")],
    )
    monkeypatch.setattr(
        "work_agent.core.sessions.thread_checkpoint_exists",
        lambda saver, tid: tid == "thread-new",
    )

    sessions = list_sessions(limit=10)
    assert [s.thread_id for s in sessions] == ["thread-new", "thread-old"]
    assert session_exists("thread-new")
    assert not session_exists("nope")
    assert not session_exists("")
