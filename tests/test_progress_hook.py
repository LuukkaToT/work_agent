"""progress ContextVar + DiagnoseProgressCallback 上报 tool 名。"""

from __future__ import annotations

from work_agent.graph.helpers.progress import (
    report_progress,
    reset_progress_hook,
    set_progress_hook,
)
from work_agent.graph.nodes.error_analysis import _DiagnoseProgressCallback


def test_report_progress_via_contextvar():
    seen: list[str] = []
    token = set_progress_hook(seen.append)
    try:
        report_progress("tool:fetch_logs")
        report_progress("node:router")
    finally:
        reset_progress_hook(token)
    assert seen == ["tool:fetch_logs", "node:router"]
    # reset 后不应再写入
    report_progress("tool:ignored")
    assert seen == ["tool:fetch_logs", "node:router"]


def test_diagnose_progress_callback_tool_start():
    seen: list[str] = []
    token = set_progress_hook(seen.append)
    try:
        cb = _DiagnoseProgressCallback()
        cb.on_tool_start({"name": "grep_logs"}, "{}")
        cb.on_chat_model_start({}, [])
    finally:
        reset_progress_hook(token)
    assert seen == ["tool:grep_logs", "status:thinking"]
