"""真实用例库适配层：映射 external SDK → CaseProvider Protocol。"""

from __future__ import annotations

from work_agent.tools.models import CaseInfo


class RealCaseProvider:
    """CaseProvider 真实实现占位（接入 external SDK 后映射）。"""

    def list_cases(self, query: str | None = None) -> list[CaseInfo]:
        """
        列出用例目录。

        参数:
            query: 可选过滤关键词。

        返回:
            CaseInfo 列表（当前未实现，抛 NotImplementedError）。
        """
        raise NotImplementedError(
            "RealCaseProvider.list_cases 未实现：请接入 external SDK 后在此映射"
        )

    def fetch_cases(self, names: list[str]) -> list[CaseInfo]:
        """
        按名拉取用例。

        参数:
            names: 用例名列表。

        返回:
            CaseInfo 列表（当前未实现，抛 NotImplementedError）。
        """
        raise NotImplementedError(
            "RealCaseProvider.fetch_cases 未实现：请接入 external SDK 后在此映射"
        )
