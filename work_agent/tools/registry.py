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
    backend = get_settings().tool_backend
    if backend == "mock":
        return MockExecutor(scenario=scenario, ticks_to_finish=2)
    raise NotImplementedError(
        f"TOOL_BACKEND={backend!r} 尚未实现，请先用 mock"
    )