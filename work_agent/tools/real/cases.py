"""真实用例库适配层：映射 external SDK → CaseProvider Protocol。"""

from __future__ import annotations

from work_agent.tools.models import CaseInfo


class RealCaseProvider:
    def list_cases(self, query: str | None = None) -> list[CaseInfo]:
        raise NotImplementedError(
            "RealCaseProvider.list_cases 未实现：请接入 external SDK 后在此映射"
        )

    def fetch_cases(self, names: list[str]) -> list[CaseInfo]:
        raise NotImplementedError(
            "RealCaseProvider.fetch_cases 未实现：请接入 external SDK 后在此映射"
        )
