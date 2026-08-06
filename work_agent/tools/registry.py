"""
依赖注入入口：根据 .env 的 TOOL_BACKEND 决定用 mock 还是 real。

节点里应写：
    from work_agent.tools.registry import get_pipeline_tool
而不是直接 import MockPipelineTool——否则切换真实实现时需要到处修改。

lru_cache：同参数复用同一实例。
这对 mock 很重要：流水线存在实例内存里，create / query 必须拿到同一个对象。
"""

from __future__ import annotations

from functools import lru_cache

from work_agent.core.config import get_settings
from work_agent.tools.mock import MockCaseProvider, MockPipelineTool, MockScenario
from work_agent.tools.protocols import CaseProvider, PipelineTool


@lru_cache(maxsize=1)
def get_case_provider() -> CaseProvider:
    backend = get_settings().tool_backend
    if backend == "mock":
        return MockCaseProvider()
    if backend == "real":
        from work_agent.tools.real import RealCaseProvider

        return RealCaseProvider()
    raise NotImplementedError(
        f"TOOL_BACKEND={backend!r} 尚未实现，请先用 mock 或 real"
    )


@lru_cache(maxsize=1)
def get_pipeline_tool(scenario: MockScenario = "all_pass") -> PipelineTool:
    """
    scenario 仅 mock 有意义（all_pass / version_fail / ...）。
    ticks_to_finish=2：第一次 query=running，第二次 finished。
    """
    backend = get_settings().tool_backend
    if backend == "mock":
        return MockPipelineTool(scenario=scenario, ticks_to_finish=2)
    if backend == "real":
        from work_agent.tools.real import RealPipelineTool

        return RealPipelineTool()
    raise NotImplementedError(
        f"TOOL_BACKEND={backend!r} 尚未实现，请先用 mock 或 real"
    )
