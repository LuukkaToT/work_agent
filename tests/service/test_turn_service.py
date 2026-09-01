"""REST/MCP 共用 TurnService 的会话边界。"""

import pytest

from work_agent.core.observability import add_event_hook, clear_event_hooks
from work_agent.service.turns import TurnService, TurnServiceError


@pytest.fixture(autouse=True)
def _memory_thread_locks(monkeypatch):
    """本文件只测 TurnService 边界，不依赖 Postgres advisory lock。"""
    monkeypatch.setattr(
        "work_agent.core.session_locks._postgres_dsn", lambda: ""
    )


@pytest.fixture
def captured_events():
    rows: list[dict] = []
    add_event_hook(rows.append)
    try:
        yield rows
    finally:
        clear_event_hooks()


def test_turn_normalizes_user_and_returns_waiting(monkeypatch, captured_events):
    seen = {}

    monkeypatch.setattr(
        "work_agent.service.turns.runtime.new_thread_id",
        lambda prefix: f"{prefix}-abc123",
    )

    def fake_run(text, *, thread_id, user_id):
        seen.update(text=text, thread_id=thread_id, user_id=user_id)
        return {
            "_thread_id": thread_id,
            "__interrupt__": [{"type": "confirm_exec", "message": "确认？"}],
            "audit": [{"private": True}],
        }

    monkeypatch.setattr("work_agent.service.turns.runtime.run_turn_step", fake_run)

    result = TurnService().turn("跑一下", user_id="Z00888363")

    assert seen == {
        "text": "跑一下",
        "thread_id": "z00888363-abc123",
        "user_id": "z00888363",
    }
    assert result.status == "waiting_input"
    assert result.interrupt[0]["type"] == "confirm_exec"
    assert "audit" not in result.model_dump()
    assert captured_events
    event = captured_events[-1]
    assert event["event"] == "turn_complete"
    assert event["thread_id"] == "z00888363-abc123"
    assert event["status"] == "waiting_input"
    assert "跑一下" not in str(event)
    assert event["interrupt_types"] == ["confirm_exec"]


def test_turn_rejects_new_message_when_thread_is_pending(monkeypatch, captured_events):
    monkeypatch.setattr(
        "work_agent.service.turns.runtime.get_turn_status",
        lambda thread_id: {
            "_thread_id": thread_id,
            "__interrupt__": [{"type": "ask_env"}],
        },
    )

    with pytest.raises(TurnServiceError, match="THREAD_PENDING"):
        TurnService().turn(
            "继续", user_id="z00888363", thread_id="z00888363-abc123"
        )
    assert captured_events[-1]["event"] == "turn_failed"
    assert captured_events[-1]["code"] == "THREAD_PENDING"


def test_turn_rejects_unknown_supplied_thread(monkeypatch):
    monkeypatch.setattr(
        "work_agent.service.turns.runtime.get_turn_status", lambda thread_id: None
    )
    with pytest.raises(TurnServiceError, match="THREAD_NOT_FOUND"):
        TurnService().turn(
            "继续", user_id="z00888363", thread_id="z00888363-missing"
        )


def test_resume_completed_returns_current_for_mcp_retry(monkeypatch):
    monkeypatch.setattr(
        "work_agent.service.turns.runtime.resume_step",
        lambda thread_id, answer: None,
    )
    monkeypatch.setattr(
        "work_agent.service.turns.runtime.get_turn_status",
        lambda thread_id: {
            "_thread_id": thread_id,
            "reply": "已完成",
            "summary": {"status": "created"},
        },
    )

    result = TurnService().resume(
        "yes",
        user_id="z00888363",
        thread_id="z00888363-abc123",
        return_current_if_completed=True,
    )

    assert result.status == "done"
    assert result.reply == "已完成"


def test_status_rejects_cross_user_thread():
    with pytest.raises(TurnServiceError, match="THREAD_FORBIDDEN"):
        TurnService().status(
            user_id="z00888363", thread_id="z00000001-abc123"
        )


@pytest.mark.parametrize("user_id", ["", "local-dev", "z123", "100888363"])
def test_invalid_user_id(user_id):
    with pytest.raises(TurnServiceError, match="INVALID_USER"):
        TurnService().turn("hello", user_id=user_id)
