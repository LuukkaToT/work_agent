"""respond 的确定性部分：事实卡片与兜底回复。LLM 路径不在这里测。"""

import json

from work_agent.graph.nodes.respond import _facts, _fallback_reply


def state_base(**overrides) -> dict:
    state = {
        "task_id": "t1",
        "user_input": "执行用例",
        "intent": "execute",
        "requirement": "",
        "analysis_path": "",
        "exec_params": {},
        "run_id": "",
        "run_status": "",
        "results": [],
        "logs": "",
        "report_path": "",
        "summary": {},
        "reply": "",
        "audit": [],
        "messages": [],
    }
    state.update(overrides)
    return state


def test_facts_drops_empty_values():
    state = state_base()
    facts = _facts(state)
    # 空 run_id / 空参数不该出现，免得模型对着空值编故事
    assert "run_id" not in facts
    assert "用例" not in facts
    assert facts["用户问题"] == "执行用例"
    assert facts["任务类型"] == "execute"


def test_facts_reads_intent_not_summary_branch():
    """任务类型只认 intent；summary 里就算还有 branch 也忽略。"""
    state = state_base(
        intent="query",
        summary={"branch": "execute", "status": "ok"},
    )
    assert _facts(state)["任务类型"] == "query"


def test_facts_keeps_zero_passed():
    """passed=0 必须保留：剔掉后「跑了但全挂」会被误读成「还没跑」。"""
    state = state_base(
        summary={
            "status": "finished",
            "total": 2,
            "passed": 0,
            "failed_count": 2,
            "failed": [
                {"case_name": "case_a", "verdict": "fail", "fail_kind": "version", "detail": "x"},
                {"case_name": "case_b", "verdict": "error", "fail_kind": "case", "detail": "y"},
            ],
        }
    )
    facts = _facts(state)
    assert facts["用例总数"] == 2
    assert facts["通过数"] == 0
    assert facts["失败数"] == 2
    assert [f["用例"] for f in facts["失败明细"]] == ["case_a", "case_b"]


def test_fallback_cancelled():
    state = state_base(
        intent="execute",
        summary={"status": "cancelled"},
    )
    reply = _fallback_reply(state, _facts(state))
    assert "取消" in reply


def test_fallback_execute_quotes_run_id_and_report():
    state = state_base(
        intent="execute",
        run_id="mock-abc123",
        run_status="finished",
        report_path="D:/x/report.md",
        summary={"status": "finished", "total": 1, "passed": 1},
    )
    reply = _fallback_reply(state, _facts(state))
    assert "mock-abc123" in reply
    assert "D:/x/report.md" in reply


def test_fallback_analysis_quotes_path():
    state = state_base(
        intent="analysis",
        analysis_path="D:/x/test_analysis.md",
        summary={"status": "ok"},
    )
    reply = _fallback_reply(state, _facts(state))
    assert "D:/x/test_analysis.md" in reply


def test_fallback_query_uses_message():
    state = state_base(
        intent="query",
        summary={"status": "not_found", "message": "台账里没有找到"},
    )
    reply = _fallback_reply(state, _facts(state))
    assert reply == "台账里没有找到"


def test_fallback_last_resort_is_valid_json():
    """走到兜底的兜底时，输出的应是合法 JSON 的事实，而不是异常。"""
    state = state_base(intent="", summary={})
    facts = _facts(state)
    reply = _fallback_reply(state, facts)
    assert json.loads(reply) == facts
