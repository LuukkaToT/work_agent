"""intake 纯函数：从 messages 提取 user_input，并归零任务级字段。"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from work_agent.graph.main_graph import intake
from work_agent.graph.state import RESET_AUDIT


@pytest.fixture(autouse=True)
def _no_real_user_config(monkeypatch):
    """默认不读真实 user_config（不依赖是否配了 Postgres）；
    需要验证读取行为的测试自己覆盖这个 patch。"""
    monkeypatch.setattr("work_agent.graph.main_graph.get_debug_mode", lambda uid: None)


def test_intake_extracts_user_input_from_last_message():
    out = intake(
        {
            "messages": [
                HumanMessage(content="上一轮"),
                AIMessage(content="上次回复"),
                HumanMessage(content="执行用例 case_a"),
            ]
        }
    )
    assert out["user_input"] == "执行用例 case_a"
    assert out["task_id"]
    assert len(out["task_id"]) == 8


def test_intake_resets_task_fields():
    """上一轮残留不得带到本轮。"""
    out = intake(
        {
            "messages": [HumanMessage(content="你好")],
            "intent": "execute",
            "pipelines": [{"pipeline_id": "pipe-old"}],
            "exec_params": {"plans": [{"case_names": ["old"]}]},
            "results": [{"case_name": "old"}],
            "analysis_path": "D:/old/analysis.md",
            "summary": {"status": "ok", "total": 9},
            "reply": "上一轮回复",
            "requirement": "旧需求",
            "dialogue_summary": "应保留的会话摘要",
        }
    )
    assert out["intent"] == ""
    assert out["requirement"] == ""
    assert out["analysis_path"] == ""
    assert out["exec_params"] == {}
    assert out["pipelines"] == []
    assert out["results"] == []
    assert out["summary"] == {}
    assert out["reply"] == ""
    assert out["debug_mode"] is False
    # messages / dialogue_summary / user_id 不在返回值里 —— 会话级，intake 绝不碰
    assert "messages" not in out
    assert "dialogue_summary" not in out
    assert "user_id" not in out
    assert "pipeline_id" not in out
    assert "report_path" not in out


def test_intake_reads_debug_mode_from_user_config(monkeypatch):
    seen_user_ids = []

    def fake_get_debug_mode(uid):
        seen_user_ids.append(uid)
        return True

    monkeypatch.setattr(
        "work_agent.graph.main_graph.get_debug_mode", fake_get_debug_mode
    )

    out = intake(
        {
            "messages": [HumanMessage(content="hi")],
            "user_id": "z00888363",
        }
    )

    assert out["debug_mode"] is True
    assert seen_user_ids == ["z00888363"]


def test_intake_defaults_debug_mode_false_when_unset(monkeypatch):
    monkeypatch.setattr(
        "work_agent.graph.main_graph.get_debug_mode", lambda uid: None
    )

    out = intake({"messages": [HumanMessage(content="hi")]})

    assert out["debug_mode"] is False


def test_intake_audit_has_reset_sentinel():
    out = intake({"messages": [HumanMessage(content="ping")]})
    audit = out["audit"]
    assert any(rec.get(RESET_AUDIT) for rec in audit)
    steps = [rec.get("step") for rec in audit if rec.get("step")]
    assert steps == ["intake"]
    assert audit[-1]["user_input"] == "ping"


def test_intake_rejects_empty_messages():
    with pytest.raises(ValueError, match="至少一条"):
        intake({"messages": []})


def test_intake_rejects_blank_content():
    with pytest.raises(ValueError, match="为空"):
        intake({"messages": [HumanMessage(content="   ")]})


def test_intake_handles_list_content():
    out = intake(
        {
            "messages": [
                HumanMessage(content=[{"type": "text", "text": "分析一下"}])
            ]
        }
    )
    assert out["user_input"] == "分析一下"
