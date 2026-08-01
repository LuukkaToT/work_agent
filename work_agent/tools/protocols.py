from __future__ import annotations

from typing import Protocol, runtime_checkable

from work_agent.tools.models import CaseInfo, CaseResult, RunHandle, RunStatus


@runtime_checkable
class CaseProvider(Protocol):
    """用例库：列目录 / 按名拉取。"""

    def list_cases(self, query: str | None = None) -> list[CaseInfo]: ...

    def fetch_cases(self, names: list[str]) -> list[CaseInfo]: ...


@runtime_checkable
class Executor(Protocol):
    """执行引擎：用例名列表 + 版本 + 逻辑组网。"""

    def run(
        self,
        case_names: list[str],
        version: str,
        topology: str,
    ) -> RunHandle: ...

    def status(self, run_id: str) -> RunStatus: ...

    def results(self, run_id: str) -> list[CaseResult]: ...

    def logs(self, run_id: str, case_name: str | None = None) -> str: ...