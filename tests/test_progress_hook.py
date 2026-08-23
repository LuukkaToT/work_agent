"""progress ContextVar 基础行为（agent_loop 内的上报单测见 test_agent_loop.py）。"""

from __future__ import annotations

from work_agent.graph.helpers.progress import (
    report_progress,
    reset_progress_hook,
    set_progress_hook,
)


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
