"""
执行收尾：

  collect_results  调 Executor.results / logs，写入 state
  write_report     副作用：落盘到 workspace/runs/<task_id>/

商用可追溯的关键：每次任务都有目录，而不是只打印在终端上。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from work_agent.core.config import get_settings
from work_agent.core.ledger import get_ledger
from work_agent.tools.registry import get_executor


def collect_results(state: Mapping[str, Any]) -> dict:
    """拉取用例结果与日志；无 run_id（例如缺组网）时跳过。"""
    run_id = state.get("run_id") or ""
    run_status = state.get("run_status") or ""

    if not run_id:
        return {
            "results": [],
            "logs": "",
            "audit": [{"step": "collect_results", "skipped": True}],
        }

    # 与 exec_poll 相同：复用已缓存的 Executor，才能靠 run_id 找到那次执行
    ex = get_executor(scenario="all_pass")
    results_data: list[dict] = []
    logs = ""

    try:
        if run_status in ("finished", "failed"):
            # asdict：dataclass → 普通 dict，方便进 state / JSON
            raw = ex.results(run_id)
            results_data = [asdict(r) for r in raw]
        logs = ex.logs(run_id)
    except Exception as exc:  # noqa: BLE001 - 学习阶段先收拢错误写入 audit
        return {
            "results": [],
            "logs": "",
            "audit": [
                {
                    "step": "collect_results",
                    "error": str(exc),
                    "run_id": run_id,
                    "run_status": run_status,
                }
            ],
        }

    passed = sum(1 for r in results_data if r.get("verdict") == "pass")
    failed = [r for r in results_data if r.get("verdict") != "pass"]

    summary = dict(state.get("summary") or {})
    summary.update(
        {
            "status": run_status or summary.get("status"),
            "total": len(results_data),
            "passed": passed,
            "failed_count": len(failed),
            "failed": failed,
        }
    )

    return {
        "results": results_data,
        "logs": logs,
        "summary": summary,
        "audit": [
            {
                "step": "collect_results",
                "run_id": run_id,
                "total": len(results_data),
                "passed": passed,
                "failed_count": len(failed),
                "logs_chars": len(logs),
            }
        ],
    }


def write_report(state: Mapping[str, Any]) -> dict:
    """
    落盘三份文件：
      task.json       任务元数据
      run_result.json 结构化结果 + 日志
      report.md       给人看的摘要
    """
    settings = get_settings()
    task_id = state.get("task_id") or "unknown"
    run_dir: Path = settings.workspace_dir / "runs" / task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    params = state.get("exec_params") or {}
    results = state.get("results") or []
    summary = dict(state.get("summary") or {})
    logs = state.get("logs") or ""

    task_doc = {
        "task_id": task_id,
        "user_input": state.get("user_input"),
        "intent": state.get("intent"),
        "exec_params": params,
        "run_id": state.get("run_id"),
        "run_status": state.get("run_status"),
        "poll_count": state.get("poll_count"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result_doc = {
        "summary": summary,
        "results": results,
        "logs": logs,
    }

    task_path = run_dir / "task.json"
    result_path = run_dir / "run_result.json"
    report_path = run_dir / "report.md"

    task_path.write_text(
        json.dumps(task_doc, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result_path.write_text(
        json.dumps(result_doc, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 下面拼 markdown，纯展示逻辑
    failed = summary.get("failed") or []
    lines = [
        f"# 执行报告 `{task_id}`",
        "",
        f"- 用户输入: {state.get('user_input')}",
        f"- run_id: {state.get('run_id') or '(无)'}",
        f"- 状态: {state.get('run_status') or summary.get('status')}",
        f"- 版本: {params.get('version', '')}",
        f"- 组网: {params.get('topology', '')}",
        f"- 用例: {', '.join(params.get('case_names') or [])}",
        f"- 轮询次数: {state.get('poll_count') or 0}",
        "",
        "## 汇总",
        "",
        f"- 总数: {summary.get('total', 0)}",
        f"- 通过: {summary.get('passed', 0)}",
        f"- 失败: {summary.get('failed_count', 0)}",
        "",
        "## 失败清单",
        "",
    ]
    if not failed:
        lines.append("(无)")
    else:
        for item in failed:
            lines.append(
                f"- `{item.get('case_name')}` "
                f"{item.get('verdict')} / {item.get('fail_kind')}: "
                f"{item.get('detail')}"
            )

    lines.extend(["", "## 日志摘录", "", "```", logs or "(无日志)", "```", ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")

    run_id = state.get("run_id") or ""
    if run_id:
        get_ledger().update_status(
            run_id,
            status=state.get("run_status") or summary.get("status") or "finished",
            report_path=str(report_path),
        )

    return {
        "report_path": str(report_path),
        "summary": summary,
        "audit": [
            {
                "step": "write_report",
                "report_path": str(report_path),
                "task_path": str(task_path),
                "result_path": str(result_path),
            }
        ],
    }
