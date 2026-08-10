"""
依赖注入入口：根据 .env 的 TOOL_BACKEND 决定用 mock 还是 real。

节点里应写：
    from work_agent.tools.registry import get_pipeline_tool
而不是直接 import MockPipelineTool——否则换公司实现时到处改。

lru_cache：同参数复用同一实例。
这对 mock 很重要：流水线存在实例内存里，create / query 必须拿到同一个对象。
"""

from __future__ import annotations

from functools import lru_cache

from work_agent.core.config import get_settings
from work_agent.tools.mock import (
    LocalCaseSheetTool,
    MockCaseProvider,
    MockLogTool,
    MockPipelineTool,
    MockScenario,
)
from work_agent.tools.protocols import CaseProvider, CaseSheetTool, LogTool, PipelineTool


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


@lru_cache(maxsize=8)
def get_log_tool(scenario: MockScenario = "case_error") -> LogTool:
    backend = get_settings().tool_backend
    if backend == "mock":
        return MockLogTool(scenario=scenario)
    if backend == "real":
        from work_agent.tools.real.logs import RealLogTool

        return RealLogTool()
    raise NotImplementedError(
        f"TOOL_BACKEND={backend!r} 尚未实现，请先用 mock 或 real"
    )

@lru_cache(maxsize=1)
def get_case_sheet_tool() -> CaseSheetTool:
    """读本地表：mock/real 共用 LocalCaseSheetTool（确定性 I/O）。"""
    return LocalCaseSheetTool()
