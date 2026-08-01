"""
依赖注入入口：根据 .env 的 TOOL_BACKEND 决定用 mock 还是 real。

节点里应写：
    from work_agent.tools.registry import get_executor
而不是直接 import MockExecutor——否则换公司实现时到处改。

lru_cache：同参数复用同一实例。
这对 mock 很重要：run 存在实例内存里，exec_run / exec_poll 必须拿到同一个对象。
"""

from __future__ import annotations

from functools import lru_cache

from work_agent.core.config import get_settings
from work_agent.tools.mock import MockCaseProvider, MockExecutor, MockScenario
from work_agent.tools.protocols import CaseProvider, Executor


@lru_cache(maxsize=1)
def get_case_provider() -> CaseProvider:
    backend = get_settings().tool_backend
    if backend == "mock":
        return MockCaseProvider()
    raise NotImplementedError(
        f"TOOL_BACKEND={backend!r} 尚未实现，请先用 mock"
    )


@lru_cache(maxsize=1)
def get_executor(scenario: MockScenario = "all_pass") -> Executor:
    """
    scenario 仅 mock 有意义（all_pass / version_fail / ...）。
    ticks_to_finish=2：第一次 status=running，第二次 finished，用来练自循环。
    """
    backend = get_settings().tool_backend
    if backend == "mock":
        return MockExecutor(scenario=scenario, ticks_to_finish=2)
    raise NotImplementedError(
        f"TOOL_BACKEND={backend!r} 尚未实现，请先用 mock"
    )
