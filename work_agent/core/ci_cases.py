"""
CI 用例目录镜像：按用例路径查逻辑组网 / 约束 / 版本。

只走 Postgres。表由 ``python -m work_agent init-db`` 创建，运行时不建表。
本模块只提供查询，不负责从公司 CI 灌数。

``lookup_ci_case(case_path)`` 是给 ``exec_flow`` 用的窄入口：命中返回记录，
未命中返回 ``None``，由调用方按「口头 > CI 表 > version_space > HITL」补参。
"""

from __future__ import annotations

from dataclasses import dataclass

from work_agent.core.db import get_pool


@dataclass(frozen=True)
class CiCaseRecord:
    """CI 表里的一条用例目录记录。"""

    case_path: str
    logic_env: str
    logic_constraint: str
    version: str

    @property
    def logical_env_complete(self) -> bool:
        """逻辑组网是否成对（环境 + 约束都有）。"""
        return bool(self.logic_env.strip() and self.logic_constraint.strip())


def lookup_ci_case(case_path: str) -> CiCaseRecord | None:
    """
    按用例路径精确查一条。

    参数:
        case_path: 用例路径（与 ``exec_params`` 里 ``case_names`` 同义）。

    返回:
        命中返回 ``CiCaseRecord``；空路径或未命中返回 ``None``。
    """
    path = (case_path or "").strip()
    if not path:
        return None
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT case_path, logic_env, logic_constraint, version
            FROM ci_cases
            WHERE case_path=%s
            """,
            (path,),
        ).fetchone()
    if not row:
        return None
    return CiCaseRecord(
        case_path=row["case_path"],
        logic_env=row["logic_env"] or "",
        logic_constraint=row["logic_constraint"] or "",
        version=row["version"] or "",
    )
