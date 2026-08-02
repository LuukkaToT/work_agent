"""intake 纯函数：从 messages 提取 user_input，并归零任务级字段。"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from work_agent.graph.main_graph import intake
from work_agent.graph.state import RESET_AUDIT


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
            "pipelines": [{"run_id": "pipe-old"}],
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
    # messages / dialogue_summary 不在返回值里 —— 会话级，intake 绝不碰
    assert "messages" not in out
    assert "dialogue_summary" not in out
    assert "run_id" not in out
    assert "report_path" not in out


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
