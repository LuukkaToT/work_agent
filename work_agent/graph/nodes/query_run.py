"""
query_run Flow：查「前面那次执行怎么样了」。

确定性流程，不是子 agent：
  1) 台账里按 run_id / 用例名 / 「上次」消解
  2) 有歧义 → interrupt 让用户选
  3) 优先读落盘 run_result.json；没有再尝试 Executor（同进程 mock 才有）
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from langgraph.types import interrupt

from work_agent.core.config import get_settings
from work_agent.core.ledger import RunRecord, get_ledger
from work_agent.graph.state import TestFlowState
from work_agent.tools.registry import get_executor


_RUN_ID_RE = re.compile(r"(mock-[a-f0-9]+|run-[a-zA-Z0-9_-]+)", re.I)
_CASE_RE = re.compile(r"(case_[a-zA-Z0-9_]+)")
_LAST_RE = re.compile(r"(上次|前面|刚才|最近一次|上一次)")


def _resolve_candidates(user_input: str) -> list[RunRecord]:
    ledger = get_ledger()
    text = user_input or ""

    m = _RUN_ID_RE.search(text)
    if m:
        rec = ledger.get(m.group(1))
        return [rec] if rec else []

    cases = _CASE_RE.findall(text)
    if cases:
        found: list[RunRecord] = []
        for name in cases:
            found.extend(ledger.find_by_case(name))
        # 去重保序
        seen: set[str] = set()
        uniq: list[RunRecord] = []
        for r in found:
            if r.run_id not in seen:
                seen.add(r.run_id)
                uniq.append(r)
        return uniq

    if _LAST_RE.search(text):
        return ledger.latest(limit=1)

    # 默认也给最近几条，便于 interrupt 选择
    return ledger.list_recent(limit=5)


def _load_disk_result(task_id: str) -> dict | None:
    path = get_settings().workspace_dir / "runs" / task_id / "run_result.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def query_run(state: TestFlowState) -> dict:
    user_input = state.get("user_input") or ""
    candidates = _resolve_candidates(user_input)

    if not candidates:
        return {
            "summary": {
                "status": "not_found",
                "message": "台账里没有找到可查询的执行记录",
            },
            "audit": [{"step": "query_run", "status": "not_found"}],
        }

    # 唯一命中直接用；多条或「最近列表」且用户没说「上次」时，若 >1 则询问
    if len(candidates) > 1 and not _LAST_RE.search(user_input):
        options = [
            {
                "run_id": r.run_id,
                "task_id": r.task_id,
                "cases": r.case_names,
                "version": r.version,
                "topology": r.topology,
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
                "summary": {
                    "status": "not_found",
                    "message": f"无效 run_id: {run_id}",
                },
                "audit": [{"step": "query_run", "status": "bad_pick", "run_id": run_id}],
            }
        record = chosen
    else:
        record = candidates[0]

    disk = _load_disk_result(record.task_id)
    results = (disk or {}).get("results") or []
    logs = (disk or {}).get("logs") or ""
    live_status = record.status

    # 同进程内 mock 还在时，补一次实时 status
    try:
        st = get_executor(scenario="all_pass").status(record.run_id)
        live_status = st.phase
    except Exception:
        pass

    exec_params = {
        "case_names": record.case_names,
        "version": record.version,
        "topology": record.topology,
    }
    failed = [r for r in results if r.get("verdict") != "pass"]
    summary = {
        "status": "ok",
        "total": len(results),
        "passed": sum(1 for r in results if r.get("verdict") == "pass"),
        "failed_count": len(failed),
        "failed": failed,
        "message": f"查询到 {record.run_id}（task={record.task_id}）",
    }

    return {
        "run_id": record.run_id,
        "run_status": live_status,
        "exec_params": exec_params,
        "results": results,
        "logs": logs,
        "report_path": record.report_path,
        "summary": summary,
        "audit": [
            {
                "step": "query_run",
                "run_id": record.run_id,
                "task_id": record.task_id,
                "from_disk": bool(disk),
            }
        ],
    }
