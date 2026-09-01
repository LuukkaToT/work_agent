"""observability：audit 摘要脱敏与 turn 事件。"""

from __future__ import annotations

from work_agent.core.observability import (
    add_event_hook,
    clear_event_hooks,
    emit_turn_event,
    summarize_audit,
)


def setup_function() -> None:
    clear_event_hooks()


def teardown_function() -> None:
    clear_event_hooks()


def test_summarize_audit_keeps_ops_fields_drops_user_text():
    audit = [
        {"step": "intake", "task_id": "t1", "user_input": "帮我分析 secret-case"},
        {"step": "router", "intent": "diagnose", "reason": "用户想看日志"},
        {
            "step": "error_analysis",
            "fail_kind": "env",
            "latency_ms": 1200,
            "token_usage": {"input": 10, "output": 4, "total": 14, "calls": 2},
            "context_chars": 800,
            "trimmed_steps": 1,
            "tool_trace": ["fetch_logs", "secret payload"],
            "selected_context_ids": ["goal", "log-1"],
        },
        {"step": "respond", "source": "fallback", "chars": 12, "error": "timeout"},
        {
            "step": "create_pipelines",
            "created": 1,
            "failed": 0,
            "pipeline_ids": ["p1"],
            "notes": {"p1": ["retry secret"]},
        },
    ]
    summary = summarize_audit(audit, interrupt_types=["confirm_exec"])
    blob = str(summary)
    assert "secret-case" not in blob
    assert "secret payload" not in blob
    assert "retry secret" not in blob
    assert summary["intent"] == "diagnose"
    assert summary["respond_source"] == "fallback"
    assert summary["diagnose"]["fail_kind"] == "env"
    assert summary["diagnose"]["latency_ms"] == 1200
    assert summary["create"]["created"] == 1
    assert summary["create"]["pipeline_id_n"] == 1
    assert "pipeline_ids" not in (summary["create"] or {})
    assert summary["interrupt_types"] == ["confirm_exec"]
    assert "tool_trace" not in (summary["diagnose"] or {})


def test_emit_turn_event_has_thread_status_not_user_message():
    captured: list[dict] = []
    add_event_hook(captured.append)
    emit_turn_event(
        op="turn",
        outcome="complete",
        user_id="z00888363",
        thread_id="z00888363-abc123",
        duration_ms=42,
        status="waiting_input",
        code="ok",
        runtime_result={
            "audit": [
                {"step": "router", "intent": "execute"},
                {"step": "intake", "user_input": "跑一下 secret"},
            ],
            "__interrupt__": [{"type": "confirm_exec", "message": "确认创建？"}],
        },
        interrupt_types=["confirm_exec"],
    )
    assert captured
    event = captured[0]
    assert event["event"] == "turn_complete"
    assert event["thread_id"] == "z00888363-abc123"
    assert event["status"] == "waiting_input"
    assert event["intent"] == "execute"
    assert event["interrupt_types"] == ["confirm_exec"]
    assert "跑一下 secret" not in str(event)
    assert "确认创建" not in str(event)
