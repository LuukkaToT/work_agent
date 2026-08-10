from work_agent.graph.helpers.diagnose_tools import build_diagnose_tools
from work_agent.graph.helpers.truncate import CharBudget
from work_agent.tools.registry import get_pipeline_tool


def test_build_returns_six_readonly_tools():
    tools = build_diagnose_tools(scenario="case_error")
    names = sorted(t.name for t in tools)
    assert names == sorted(
        [
            "get_pipeline_status",
            "fetch_logs",
            "grep_logs",
            "find_case_history",
            "get_case_spec",
            "search_knowledge",
        ]
    )
    assert "create" not in names
    assert "start" not in names


def test_fetch_logs_and_status_callable():
    pipe = get_pipeline_tool(scenario="case_error")
    handle = pipe.create(
        case_names=["case_downlink_001"],
        version="27B",
        env="7.223.50.60",
    )
    pipe.start(handle.pipeline_id)

    tools = {t.name: t for t in build_diagnose_tools(scenario="case_error")}
    status = tools["get_pipeline_status"].invoke({"pipeline_id": handle.pipeline_id})
    logs = tools["fetch_logs"].invoke(
        {"pipeline_id": handle.pipeline_id, "tail_lines": 50}
    )
    assert "phase" in status or "pipeline_id" in status
    assert "[log meta]" in logs or "ERROR" in logs or "KeyError" in logs


def test_search_knowledge_hits():
    tools = {t.name: t for t in build_diagnose_tools()}
    out = tools["search_knowledge"].invoke(
        {"query": "CELL_BAND unresolved", "top_k": 2}
    )
    assert "no hits" not in out
    assert "### " in out or "knowledge" in out


def test_budget_can_block():
    budget = CharBudget(limit=50)
    tools = {
        t.name: t
        for t in build_diagnose_tools(
            budget=budget,
            tool_result_max_chars=40,
            scenario="case_error",
        )
    }
    pipe = get_pipeline_tool(scenario="case_error")
    handle = pipe.create(
        case_names=["case_downlink_001"],
        version="27B",
        env="7.223.50.60",
    )
    # 连续调用，第二次起容易触预算
    tools["fetch_logs"].invoke({"pipeline_id": handle.pipeline_id, "tail_lines": 200})
    second = tools["fetch_logs"].invoke(
        {"pipeline_id": handle.pipeline_id, "tail_lines": 200}
    )
    assert "budget exceeded" in second or len(second) <= 50 or "[truncated" in second