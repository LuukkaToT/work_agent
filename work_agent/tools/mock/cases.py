"""Mock 用例库：内存固定目录。"""

from __future__ import annotations

from work_agent.tools.models import CaseInfo


_CASES: list[CaseInfo] = [
    CaseInfo("case_downlink_001", "256T 下行基础连通", ["downlink", "smoke"]),
    CaseInfo("case_downlink_002", "256T 下行压力", ["downlink", "stress"]),
    CaseInfo("case_uplink_001", "上行注册与附着", ["uplink"]),
    CaseInfo("case_env_probe", "环境健康探测", ["env"]),
]


class MockCaseProvider:
    """实现 CaseProvider：内存假用例库。"""

    def list_cases(self, query: str | None = None) -> list[CaseInfo]:
        """
        列出用例；可按关键词过滤。

        参数:
            query: 过滤关键词；None 返回全部。

        返回:
            CaseInfo 列表。
        """
        if not query:
            return list(_CASES)
        q = query.lower()
        return [
            c
            for c in _CASES
            if q in c.name.lower()
            or q in c.title.lower()
            or any(q in t for t in c.tags)
        ]

    def fetch_cases(self, names: list[str]) -> list[CaseInfo]:
        """
        按名拉取用例详情。

        参数:
            names: 用例名列表。

        返回:
            CaseInfo 列表；缺名抛 KeyError。
        """
        by_name = {c.name: c for c in _CASES}
        missing = [n for n in names if n not in by_name]
        if missing:
            raise KeyError(f"用例不存在: {missing}")
        return [by_name[n] for n in names]
