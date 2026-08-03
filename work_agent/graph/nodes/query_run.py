"""
query_run Flow：查「前面那次执行怎么样了」。

  1) 台账消解 pipeline_id / 用例名 / 「上次」
  2) 有歧义 → interrupt
  3) 逐个 PipelineTool.query
  4) 同一 task 多条一并聚合
"""

from __future__ import annotations

from dataclasses import asdict

from work_agent.core.ledger import get_ledger
from work_agent.graph.nodes.pipeline_resolve import pick_records_for_action
from work_agent.graph.state import TestFlowState
from work_agent.tools.registry import get_pipeline_tool


def query_run(state: TestFlowState) -> dict:
    user_input = state.get("user_input") or ""
    records = pick_records_for_action(user_input, action="查询")

    if not records:
        return {
            "pipelines": [],
            "results": [],
            "summary": {
                "status": "not_found",
                "message": "台账里没有找到可查询的流水线",
            },
            "audit": [{"step": "query_run", "status": "not_found"}],
        }

    tool = get_pipeline_tool(scenario="all_pass")
    pipelines: list[dict] = []
    all_results: list[dict] = []

    for record in records:
        live_status = record.status
        results: list[dict] = []
        message = ""
        try:
            pr = tool.query(record.pipeline_id)
            live_status = pr.phase
            results = [asdict(r) for r in pr.results]
            message = pr.message
            get_ledger().update_status(record.pipeline_id, status=pr.phase)
        except Exception as exc:  # noqa: BLE001
            message = f"无法实时查询: {exc}"

        pipelines.append(
            {
                "pipeline_id": record.pipeline_id,
                "case_names": record.case_names,
                "version": record.version,
                "env": record.env,
                "status": live_status,
                "error": "",
                "message": message,
            }
        )
        all_results.extend(results)

    failed = [r for r in all_results if r.get("verdict") != "pass"]
    pids = [p["pipeline_id"] for p in pipelines]
    summary = {
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
        "exec_params": {
            "plans": [
                {
                    "case_names": p["case_names"],
                    "version": p["version"],
                    "env": p["env"],
                }
                for p in pipelines
            ]
        },
        "summary": summary,
        "audit": [
            {
                "step": "query_run",
                "pipeline_ids": pids,
                "pipeline_count": len(pipelines),
                "result_count": len(all_results),
            }
        ],
    }
