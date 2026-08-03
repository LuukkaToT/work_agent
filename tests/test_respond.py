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
        "pipelines": [],
        "results": [],
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
    assert "流水线" not in facts
    assert "用例" not in facts
    assert facts["用户问题"] == "执行用例"
    assert facts["任务类型"] == "execute"


def test_facts_reads_intent_not_summary_branch():
    state = state_base(
        intent="query",
        summary={"branch": "execute", "status": "ok"},
    )
    assert _facts(state)["任务类型"] == "query"


def test_facts_keeps_zero_passed():
    state = state_base(
        summary={
            "status": "ok",
            "total": 2,
            "passed": 0,
            "failed_count": 2,
            "failed": [
                {
                    "case_name": "case_a",
                    "verdict": "fail",
                    "fail_kind": "version",
                    "detail": "x",
                },
                {
                    "case_name": "case_b",
                    "verdict": "error",
                    "fail_kind": "case",
                    "detail": "y",
                },
            ],
        }
    )
    facts = _facts(state)
    assert facts["用例总数"] == 2
    assert facts["通过数"] == 0
    assert facts["失败数"] == 2
    assert [f["用例"] for f in facts["失败明细"]] == ["case_a", "case_b"]


def test_facts_includes_pipelines():
    state = state_base(
        pipelines=[
            {
                "pipeline_id": "pipe-aaa",
                "env": "7.223.50.60",
                "version": "27B",
                "case_names": ["HF_20B_PUSCH_001"],
                "status": "running",
                "error": "",
            }
        ],
        summary={"status": "submitted", "created": 1},
    )
    facts = _facts(state)
    assert facts["pipeline_id列表"] == ["pipe-aaa"]
    assert facts["流水线"][0]["环境"] == "7.223.50.60"
    assert facts["已处理流水线数"] == 1


def test_fallback_cancelled():
    state = state_base(
        intent="execute",
        summary={"status": "cancelled"},
    )
    reply = _fallback_reply(state, _facts(state))
    assert "取消" in reply


def test_fallback_execute_lists_pipelines():
    state = state_base(
        intent="execute",
        pipelines=[
            {
                "pipeline_id": "pipe-abc123",
                "env": "7.223.50.60",
                "status": "running",
            },
            {
                "pipeline_id": "pipe-def456",
                "env": "7.223.60.11",
                "status": "running",
            },
        ],
        summary={"status": "submitted", "created": 2, "failed_pipelines": 0},
    )
    reply = _fallback_reply(state, _facts(state))
    assert "pipe-abc123" in reply
    assert "pipe-def456" in reply
    assert "流水线前端" in reply


def test_fallback_create_only():
    state = state_base(
        intent="execute",
        exec_params={"exec_mode": "create_only"},
        pipelines=[
            {
                "pipeline_id": "pipe-only",
                "env": "7.223.50.60",
                "status": "created",
            }
        ],
        summary={"status": "created", "created": 1, "exec_mode": "create_only"},
    )
    reply = _fallback_reply(state, _facts(state))
    assert "尚未启动" in reply
    assert "pipe-only" in reply


def test_fallback_start_intent():
    state = state_base(
        intent="start",
        pipelines=[{"pipeline_id": "pipe-s1", "status": "running"}],
        summary={"status": "submitted", "message": "已启动 1 条流水线"},
    )
    reply = _fallback_reply(state, _facts(state))
    assert "已启动" in reply


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
    state = state_base(intent="", summary={})
    facts = _facts(state)
    reply = _fallback_reply(state, facts)
    assert json.loads(reply) == facts
