"""
query_run Flow：查「前面那次执行怎么样了」。

确定性流程，不是子 agent：
  1) 台账里按 run_id / 用例名 / 「上次」消解
  2) 有歧义 → interrupt 让用户选
  3) 调 PipelineTool.query_result 拿实时数据
  4) 同一 task 的多条流水线一并查询并聚合
"""

from __future__ import annotations

import re
from dataclasses import asdict

from langgraph.types import interrupt

from work_agent.core.ledger import RunRecord, get_ledger
from work_agent.graph.state import TestFlowState
from work_agent.tools.registry import get_pipeline_tool


_RUN_ID_RE = re.compile(r"(pipe-[a-f0-9]+|mock-[a-f0-9]+|run-[a-zA-Z0-9_-]+)", re.I)
# 长用例名：字母开头，含下划线/连字符，至少 8 字符
_CASE_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]{7,})\b")
_LAST_RE = re.compile(r"(上次|前面|刚才|最近一次|上一次|那两?条|那几条)")


def _resolve_candidates(user_input: str) -> list[RunRecord]:
    ledger = get_ledger()
    text = user_input or ""

    m = _RUN_ID_RE.search(text)
    if m:
        rec = ledger.get(m.group(1))
        return [rec] if rec else []

    cases = _CASE_RE.findall(text)
    # 过滤掉像 version 标签之类的短命中噪声：保留含下划线的
    cases = [c for c in cases if "_" in c or "-" in c]
    if cases:
        found: list[RunRecord] = []
        for name in cases:
            found.extend(ledger.find_by_case(name))
        seen: set[str] = set()
        uniq: list[RunRecord] = []
        for r in found:
            if r.run_id not in seen:
                seen.add(r.run_id)
                uniq.append(r)
        return uniq

    if _LAST_RE.search(text):
        latest = ledger.latest(limit=1)
        if not latest:
            return []
        # 同一 task 可能有多条流水线（多环境），一并返回
        siblings = ledger.find_by_task(latest[0].task_id)
        return siblings or latest

    return ledger.list_recent(limit=5)


def query_run(state: TestFlowState) -> dict:
    user_input = state.get("user_input") or ""
    candidates = _resolve_candidates(user_input)

    if not candidates:
        return {
            "pipelines": [],
            "results": [],
            "summary": {
                "status": "not_found",
                "message": "台账里没有找到可查询的执行记录",
            },
            "audit": [{"step": "query_run", "status": "not_found"}],
        }

    # 唯一命中，或「上次/那几条」已消解到同一 task：直接用
    # 多条且用户没说「上次」：若来自 list_recent 且 >1，询问
    if len(candidates) > 1 and not _LAST_RE.search(user_input):
        # 若全部同 task，视为一次多环境查询，不必挑
        task_ids = {r.task_id for r in candidates}
        if len(task_ids) != 1:
            options = [
                {
                    "run_id": r.run_id,
                    "task_id": r.task_id,
                    "cases": r.case_names,
                    "version": r.version,
                    "env": r.env,
                    "status": r.status,
                    "created_at": r.created_at,
                }
                for r in candidates[:8]
            ]
            reply = interrupt(
                {
                    "type": "pick_run",
                    "message": "找到多条执行记录，请输入要查询的 run_id",
                    "options": options,
                }
            )
            run_id = str(reply).strip()
            chosen = next((r for r in candidates if r.run_id == run_id), None)
            if chosen is None:
                chosen = get_ledger().get(run_id)
            if chosen is None:
                return {
                    "pipelines": [],
                    "results": [],
                    "summary": {
                        "status": "not_found",
                        "message": f"无效 run_id: {run_id}",
                    },
                    "audit": [
                        {"step": "query_run", "status": "bad_pick", "run_id": run_id}
                    ],
                }
            # 同 task 的兄弟流水线一并查
            records = get_ledger().find_by_task(chosen.task_id) or [chosen]
        else:
            records = candidates
    else:
        records = candidates

    tool = get_pipeline_tool(scenario="all_pass")
    pipelines: list[dict] = []
    all_results: list[dict] = []

    for record in records:
        live_status = record.status
        results: list[dict] = []
        message = ""
        try:
            pr = tool.query_result(record.run_id)
            live_status = pr.phase
            results = [asdict(r) for r in pr.results]
            message = pr.message
            get_ledger().update_status(record.run_id, status=pr.phase)
        except Exception as exc:  # noqa: BLE001 - 跨进程 mock 可能已丢
            message = f"无法实时查询: {exc}"

        pipelines.append(
            {
                "run_id": record.run_id,
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
    run_ids = [p["run_id"] for p in pipelines]
    summary = {
        "status": "ok",
        "total": len(all_results),
        "passed": sum(1 for r in all_results if r.get("verdict") == "pass"),
        "failed_count": len(failed),
        "failed": failed,
        "message": (
            f"查询到 {len(pipelines)} 条流水线："
            + ", ".join(run_ids)
        ),
    }

    # 若尚未出结果（仍在 running），结果列表可能为空
    if not all_results and any(p["status"] == "running" for p in pipelines):
        summary["message"] = (
            f"流水线仍在执行中：{', '.join(run_ids)}。"
            "最终结果请到流水线前端查看，也可稍后再问。"
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
                "run_ids": run_ids,
                "pipeline_count": len(pipelines),
                "result_count": len(all_results),
            }
        ],
    }
