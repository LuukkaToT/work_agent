"""
公司系统的「插座」定义。

图和节点只依赖这里的 Protocol，不依赖 mock 或 real 的类名。
接公司 tool 时：在 tools/real/ 写实现 → registry 切换 → 图不用改。

@runtime_checkable：允许 isinstance(obj, CaseProvider) 做运行时检查（lesson 里用过）。
"""

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
    """
    执行引擎三要素：用例名列表 + 版本 + 逻辑组网。
    单个执行 = 列表长度为 1，不必单独做单跑接口。
    """

    def run(
        self,
        case_names: list[str],
        version: str,
        topology: str,
    ) -> RunHandle: ...

    def status(self, run_id: str) -> RunStatus: ...

    def results(self, run_id: str) -> list[CaseResult]: ...

    def logs(self, run_id: str, case_name: str | None = None) -> str: ...
