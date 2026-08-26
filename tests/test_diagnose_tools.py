import uuid

from work_agent.graph.helpers.diagnose_tools import build_diagnose_tools
from work_agent.graph.helpers.truncate import CharBudget
from work_agent.tools.registry import get_pipeline_tool


def test_build_diagnose_tools_passes_policy_self_check():
    """构造期会跑 assert_read_only_whitelist；6 个工具都在只读白名单里，不应抛异常。"""
    build_diagnose_tools()


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
        physical_env="7.223.50.60",
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


def test_find_case_history_filters_by_user_id(pg_ledger, make_user_id):
    """user_id 只在自己名下的记录里查得到，看不到别人的。"""
    alice = make_user_id()
    bob = make_user_id()
    case_name = f"case_dl_{uuid.uuid4().hex[:10]}"
    pid_alice = f"p-{uuid.uuid4().hex[:10]}"
    pid_bob = f"p-{uuid.uuid4().hex[:10]}"
    pg_ledger.upsert(
        pipeline_id=pid_alice,
        task_id="t1",
        case_names=[case_name],
        version="27B",
        env="7.223.50.60",
        status="running",
        user_id=alice,
    )
    pg_ledger.upsert(
        pipeline_id=pid_bob,
        task_id="t2",
        case_names=[case_name],
        version="27B",
        env="7.223.60.11",
        status="running",
        user_id=bob,
    )

    alice_tools = {t.name: t for t in build_diagnose_tools(user_id=alice)}
    out = alice_tools["find_case_history"].invoke({"case_name": case_name})
    assert pid_alice in out
    assert pid_bob not in out

    empty_tools = {t.name: t for t in build_diagnose_tools()}  # user_id 默认空串
    out_empty = empty_tools["find_case_history"].invoke({"case_name": case_name})
    assert "no records" in out_empty


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
        physical_env="7.223.50.60",
    )
    # 连续调用，第二次起容易触预算
    tools["fetch_logs"].invoke({"pipeline_id": handle.pipeline_id, "tail_lines": 200})
    second = tools["fetch_logs"].invoke(
        {"pipeline_id": handle.pipeline_id, "tail_lines": 200}
    )
    assert "budget exceeded" in second or len(second) <= 50 or "[truncated" in second


def test_archive_registers_readback_tool(tmp_path):
    """带 archive 构造：第 7 个工具出现且过自检；不带则保持 6 个。"""
    from work_agent.graph.helpers.context_archive import ContextArchive

    with_archive = build_diagnose_tools(scenario="case_error", archive=ContextArchive(tmp_path, run_id="t"))
    assert "fetch_archived_block" in {t.name for t in with_archive}

    without_archive = build_diagnose_tools(scenario="case_error")
    assert "fetch_archived_block" not in {t.name for t in without_archive}


def test_fetch_archived_block_reads_and_reports_missing(tmp_path):
    """正常 id 回读原文；未知/不存在 id 返回错误字符串而不是抛异常。"""
    from work_agent.graph.helpers.context_archive import ContextArchive

    archive = ContextArchive(tmp_path, run_id="t")
    tools = {t.name: t for t in build_diagnose_tools(scenario="case_error", archive=archive)}

    missing = tools["fetch_archived_block"].invoke({"artifact_id": "no_such_artifact"})
    assert "[fetch_archived_block error]" in missing

    ref = archive.store(
        _archived_item("obs-001-fetch_logs", "ERROR KeyError: 'antenna_map'\n" + "d" * 300)
    )
    got = tools["fetch_archived_block"].invoke({"artifact_id": ref.artifact_id})
    assert "KeyError: 'antenna_map'" in got


def _archived_item(item_id: str, text: str):
    from work_agent.graph.helpers.context_selector import PIN_NORMAL, ContextItem

    return ContextItem(
        item_id=item_id,
        kind="tool_result",
        source="fetch_logs",
        text=text,
        priority=4,
        pin=PIN_NORMAL,
    )