"""已有流水线操作节点：消解 → start / query / diagnose。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from work_agent.core.ledger import get_ledger
from work_agent.graph.helpers.pipeline_resolve import (
    pick_records_for_action,
    records_to_pipeline_dicts,
)
from work_agent.tools.registry import get_pipeline_tool

_OPS_ACTIONS = {
    "start": "启动",
    "query": "查询",
    "diagnose": "诊断",
}


def init_ops_kind(state: Mapping[str, Any]) -> dict:
    """
    把主图 intent（start|query|diagnose）落入子图私有 ops_kind。

    参数:
        state: 可读 ``ops_kind`` 或 ``intent``。

    返回:
        规范化后的 ``ops_kind`` 与 audit。
    """
    kind = str(state.get("ops_kind") or state.get("intent") or "query").strip()
    if kind not in _OPS_ACTIONS:
        kind = "query"
    return {
        "ops_kind": kind,
        "audit": [{"step": "init_ops_kind", "ops_kind": kind}],
    }


def resolve_pipelines(state: Mapping[str, Any]) -> dict:
    """
    按 ops_kind 从台账消解目标流水线，写入 pipelines。

    参数:
        state: 读 ``ops_kind`` / ``intent`` / ``user_input`` / ``user_id``。

    返回:
        ``pipelines``（可能为空）、必要时的 not_found ``summary``，以及 audit。
    """
    kind = str(state.get("ops_kind") or state.get("intent") or "query")
    if kind not in _OPS_ACTIONS:
        kind = "query"
    action = _OPS_ACTIONS[kind]
    user_input = state.get("user_input") or ""
    user_id = state.get("user_id") or ""
    records = pick_records_for_action(user_input, action=action, user_id=user_id)

    if not records:
        return {
            "ops_kind": kind,
            "pipelines": [],
            "results": [],
            "summary": {
                "status": "not_found",
                "message": f"台账里没有找到可{action}的流水线",
            },
            "audit": [{"step": "resolve_pipelines", "status": "not_found", "ops_kind": kind}],
        }

    pipelines = records_to_pipeline_dicts(records)
    return {
        "ops_kind": kind,
        "pipelines": pipelines,
        "audit": [
            {
                "step": "resolve_pipelines",
                "ops_kind": kind,
                "pipeline_ids": [p["pipeline_id"] for p in pipelines],
            }
        ],
    }


def route_after_resolve(state: Mapping[str, Any]) -> str:
    """
    消解后条件边：无 pipelines → skip；否则按 ops_kind 分支。

    参数:
        state: 读 ``pipelines`` / ``ops_kind``。

    返回:
        ``skip`` / ``start`` / ``query`` / ``diagnose``。
    """
    if not (state.get("pipelines") or []):
        return "skip"
    kind = str(state.get("ops_kind") or "query")
    if kind in ("start", "query", "diagnose"):
        return kind
    return "query"


def query_pipelines(state: Mapping[str, Any]) -> dict:
    """
    对 state.pipelines 逐个 query（不再二次消解）。

    参数:
        state: 读已消解的 ``pipelines``。

    返回:
        更新后的 ``pipelines`` / ``results`` / 聚合 ``summary`` 与 audit。
    """
    items = list(state.get("pipelines") or [])
    if not items:
        return {
            "pipelines": [],
            "results": [],
            "summary": {
                "status": "not_found",
                "message": "台账里没有找到可查询的流水线",
            },
            "audit": [{"step": "query_pipelines", "status": "empty"}],
        }

    tool = get_pipeline_tool(scenario="all_pass")
    pipelines: list[dict] = []
    all_results: list[dict] = []

    for item in items:
        pid = str(item.get("pipeline_id") or "")
        live_status = item.get("status") or ""
        results: list[dict] = []
        message = ""
        try:
            pr = tool.query(pid)
            live_status = pr.phase
            results = [asdict(r) for r in pr.results]
            message = pr.message
            get_ledger().update_status(pid, status=pr.phase)
        except Exception as exc:  # noqa: BLE001
            message = f"无法实时查询: {exc}"

        pipelines.append(
            {
                "pipeline_id": pid,
                "case_names": item.get("case_names") or [],
                "version": item.get("version") or "",
                "env": item.get("env") or "",
                "status": live_status,
                "error": "",
                "message": message,
            }
        )
        all_results.extend(results)

    failed = [r for r in all_results if r.get("verdict") != "pass"]
    pids = [p["pipeline_id"] for p in pipelines]
    summary: dict[str, Any] = {
        "status": "ok",
        "total": len(all_results),
        "passed": sum(1 for r in all_results if r.get("verdict") == "pass"),
        "failed_count": len(failed),
        "failed": failed,
        "message": f"查询到 {len(pipelines)} 条流水线：" + ", ".join(pids),
    }

    if not all_results and any(p["status"] == "running" for p in pipelines):
        summary["message"] = (
            f"流水线仍在执行中：{', '.join(pids)}。"
            "最终结果请到流水线前端查看，也可稍后再问。"
        )
    if not all_results and any(p["status"] == "created" for p in pipelines):
        summary["message"] = (
            f"流水线已创建尚未启动：{', '.join(pids)}。"
            "可以说「启动刚才那几条」来启动。"
        )

    return {
        "pipelines": pipelines,
        "results": all_results,
        "summary": summary,
        "audit": [
            {
                "step": "query_pipelines",
                "pipeline_ids": pids,
                "pipeline_count": len(pipelines),
                "result_count": len(all_results),
            }
        ],
    }
