"""Mock 日志 tool：按 pipeline_id 返回假日志。"""

from __future__ import annotations


class MockLogTool:
    def fetch_logs(self, pipeline_id: str) -> str:
        pid = (pipeline_id or "").strip() or "(empty)"
        return (
            f"[mock-log] pipeline_id={pid}\n"
            "2026-08-09 10:00:01 INFO start case CaseA_235T_nmimo\n"
            "2026-08-09 10:00:05 ERROR AssertionError: KPI below threshold\n"
            "2026-08-09 10:00:05 ERROR Traceback (most recent call last):\n"
            "  File \"case_runner.py\", line 42, in run\n"
            "    assert kpi > 0.99\n"
            "2026-08-09 10:00:06 INFO case finished verdict=fail\n"
        )
