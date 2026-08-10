"""
诊断期只读 Tool 工厂（ReAct 加厚核心）。

禁止注册 create / start。
面向用户的 query 仍走 pipeline_ops 图节点；这里的 get_pipeline_status 只是诊断窥视。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from langchain_core.tools import BaseTool, tool

from work_agent.core.ledger import get_ledger
from work_agent.graph.helpers.truncate import CharBudget
from work_agent.tools.mock.executor import MockScenario
from work_agent.tools.registry import (
    get_case_provider,
    get_knowledge_search_tool,
    get_log_tool,
    get_pipeline_tool,
)

_DEFAULT_MAX_CHARS = 8000


def build_diagnose_tools(
    *,
    scenario: MockScenario = "case_error",
    budget: CharBudget | None = None,
    tool_result_max_chars: int = _DEFAULT_MAX_CHARS,
) -> list[BaseTool]:
    """
    构造诊断白名单工具（只读；禁止 create/start）。

    pipeline / log 使用同一 scenario，避免状态与日志对不上。

    参数:
        scenario: mock 故障场景，同时作用于 pipeline 与 log。
        budget: 本轮工具返回累计字符预算；None 用默认。
        tool_result_max_chars: 单次工具结果截断上限。

    返回:
        LangChain BaseTool 列表，供 ReAct agent 使用。
    """
    if budget is None:
        budget = CharBudget(limit=40_000)

    pipeline_tool = get_pipeline_tool(scenario=scenario)
    log_tool = get_log_tool(scenario=scenario)
    case_provider = get_case_provider()
    knowledge = get_knowledge_search_tool()
    ledger = get_ledger()
    max_chars = tool_result_max_chars

    def _out(text: str) -> str:
        """经预算截断后返回工具文本。"""
        return budget.take(text, max_chars=max_chars)

    @tool
    def get_pipeline_status(pipeline_id: str) -> str:
        """查看流水线当前 phase 与各用例 verdict（只读）。诊断开始时优先调用。"""
        try:
            pr = pipeline_tool.query(pipeline_id)
            payload = {
                "pipeline_id": pr.pipeline_id,
                "phase": pr.phase,
                "message": pr.message,
                "results": [asdict(r) for r in pr.results],
            }
            return _out(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            return _out(f"[get_pipeline_status error] {exc}")

    @tool
    def fetch_logs(pipeline_id: str, tail_lines: int = 200) -> str:
        """拉取流水线日志尾部（只读）。默认 200 行；需要定位关键词时改用 grep_logs。"""
        try:
            text = log_tool.fetch_logs(pipeline_id, tail_lines=tail_lines)
            return _out(text)
        except Exception as exc:  # noqa: BLE001
            return _out(f"[fetch_logs error] {exc}")

    @tool
    def grep_logs(
        pipeline_id: str,
        pattern: str,
        context_lines: int = 3,
        max_matches: int = 20,
    ) -> str:
        """在日志全文检索 pattern（只读），返回命中及上下文。比整包 fetch 更省上下文。"""
        try:
            text = log_tool.grep_logs(
                pipeline_id,
                pattern,
                context_lines=context_lines,
                max_matches=max_matches,
            )
            return _out(text)
        except Exception as exc:  # noqa: BLE001
            return _out(f"[grep_logs error] {exc}")

    @tool
    def find_case_history(case_name: str, limit: int = 5) -> str:
        """查该用例近期流水线台账（只读），用于区分偶发失败 vs 持续失败。"""
        try:
            rows = ledger.find_by_case(case_name, limit=limit)
            payload: list[dict[str, Any]] = [
                {
                    "pipeline_id": r.pipeline_id,
                    "status": r.status,
                    "version": r.version,
                    "env": r.env,
                    "case_names": r.case_names,
                    "created_at": r.created_at,
                }
                for r in rows
            ]
            if not payload:
                return _out(f"[find_case_history] no records for case={case_name!r}")
            return _out(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            return _out(f"[find_case_history error] {exc}")

    @tool
    def get_case_spec(case_name: str) -> str:
        """获取用例元信息（名称/标题/标签），只读。"""
        try:
            cases = case_provider.fetch_cases([case_name])
            payload = [asdict(c) for c in cases]
            return _out(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            return _out(f"[get_case_spec error] {exc}")

    @tool
    def search_knowledge(query: str, top_k: int = 3) -> str:
        """检索故障知识/手册（旁证，只读）。主证据仍必须来自日志；不可用检索结果编造日志原文。"""
        try:
            text = knowledge.search(query, top_k=top_k)
            return _out(text)
        except Exception as exc:  # noqa: BLE001
            return _out(f"[search_knowledge error] {exc}")

    return [
        get_pipeline_status,
        fetch_logs,
        grep_logs,
        find_case_history,
        get_case_spec,
        search_knowledge,
    ]