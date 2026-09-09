import uuid

import pytest

from work_agent.graph.helpers.diagnose_tools import build_diagnose_tools
from work_agent.graph.helpers.truncate import CharBudget
from work_agent.tools.registry import get_pipeline_tool


def test_build_diagnose_tools_passes_policy_self_check():
    """构造期会跑 assert_read_only_whitelist；6 个工具都在只读白名单里，不应抛异常。"""
    build_diagnose_tools()


def test_build_returns_eight_readonly_tools():
    tools = build_diagnose_tools(scenario="case_error")
    names = sorted(t.name for t in tools)
    assert names == sorted(
        [
            "get_pipeline_status",
            "list_log_files",
            "fetch_logs",
            "grep_logs",
            "lookup_error_code",
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


def test_layered_log_tools_can_list_filter_and_lookup_codes():
    scenario = "bench12_vue_antenna_map_missing"
    pipe = get_pipeline_tool(scenario=scenario)
    handle = pipe.create(
        case_names=["NR_BENCH_012_VUE_ANTENNA_MAP"],
        version="27B",
        physical_env="7.223.50.71",
    )
    pipe.start(handle.pipeline_id)
    tools = {t.name: t for t in build_diagnose_tools(scenario=scenario)}

    listed = tools["list_log_files"].invoke({"pipeline_id": handle.pipeline_id})
    assert all(f'"{name}"' in listed for name in ("comm", "rat", "bbh", "bbl", "marp", "compare"))

    marp = tools["fetch_logs"].invoke(
        {"pipeline_id": handle.pipeline_id, "tail_lines": 30, "component": "marp"}
    )
    assert "E-MARP-4106" in marp
    assert "[MARP" in marp
    assert "[BBL" not in marp

    code = tools["lookup_error_code"].invoke({"code": "E-MARP-4106"})
    assert "ANTENNA_MAP_MISSING" in code


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
    """带 archive 构造：额外回读工具出现且过自检。"""
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


def test_fetch_archived_block_excerpts_long_original(tmp_path):
    from work_agent.graph.helpers.context_archive import ContextArchive

    archive = ContextArchive(tmp_path, run_id="t")
    tools = {t.name: t for t in build_diagnose_tools(scenario="case_error", archive=archive)}
    raw = "2026-09-09 10:00:00 INFO padding\n" * 200
    raw += "2026-09-09 10:05:01 ERROR KeyError: 'antenna_map'\n"
    raw += "2026-09-09 10:05:02 INFO padding\n" * 200
    ref = archive.store(_archived_item("obs-long", raw))
    got = tools["fetch_archived_block"].invoke({"artifact_id": ref.artifact_id})
    assert "KeyError: 'antenna_map'" in got
    assert len(got) < len(raw)
    assert archive.read(ref.artifact_id) == raw


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


def test_scope_lock_tools_force_pipeline_and_reject_foreign_component():
    from work_agent.graph.helpers.diagnose_tools import (
        apply_tool_scope,
        scope_lock_tools,
    )

    locked = apply_tool_scope(
        "fetch_logs",
        {"pipeline_id": "p1", "tail_lines": 20},
        pipeline_id="p1",
        component="marp",
    )
    assert locked["component"] == "marp"
    tools = {
        t.name: t
        for t in scope_lock_tools(
            build_diagnose_tools(scenario="case_error"),
            pipeline_id="p1",
            component="marp",
            allowed_names=["fetch_logs", "grep_logs", "search_knowledge"],
        )
    }
    assert set(tools) == {"fetch_logs", "grep_logs", "search_knowledge"}
    with pytest.raises(ValueError, match="越界"):
        tools["grep_logs"].invoke({"pipeline_id": "p1", "pattern": "x", "component": "bbh"})
