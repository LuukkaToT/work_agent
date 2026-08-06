"""测试分析兼容入口。

主图已经直接挂载独立子图；保留此函数仅兼容可能存在的旧调用方。
"""

from __future__ import annotations

from work_agent.graph.state import TestFlowState
from work_agent.graph.subgraphs.analysis_flow import build_test_analysis_graph


def test_analysis(state: TestFlowState) -> dict:
    return build_test_analysis_graph().invoke(
        {
            "task_id": state.get("task_id") or "unknown",
            "user_input": state.get("user_input") or state.get("requirement") or "",
        }
    )
