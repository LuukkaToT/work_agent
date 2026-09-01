"""CI 用例目录：用例默认环境和负责人。"""

from __future__ import annotations

from dataclasses import dataclass

from work_agent.core.db import get_pool
from work_agent.core.logic_topologies import LogicTopologyRecord, record_from_row


@dataclass(frozen=True)
class CiCaseRecord:
    case_name: str
    physical_topology: str = ""
    logic_topology: LogicTopologyRecord | None = None
    owner: str = ""

    @property
    def case_path(self) -> str:
        return self.case_name

    @property
    def logic_env(self) -> str:
        return self.logic_topology.name if self.logic_topology else ""

    @property
    def logic_constraint(self) -> str:
        return self.logic_topology.constraint if self.logic_topology else ""

    @property
    def version(self) -> str:
        """兼容旧调用；CI 不再提供版本。"""
        return ""

    @property
    def logical_env_complete(self) -> bool:
        return self.logic_topology is not None


def lookup_ci_case(case_name: str) -> CiCaseRecord | None:
    name = (case_name or "").strip()
    if not name:
        return None
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT c.case_name, c.physical_topology, c.owner,
                   lt.id, lt.name, lt.constraint_value AS topology_constraint,
                   lt.config, lt.aliases
            FROM ci_cases AS c
            LEFT JOIN logic_topologies AS lt ON lt.id=c.logic_topology_id
            WHERE c.case_name=%s
            """,
            (name,),
        ).fetchone()
    if not row:
        return None
    topology = record_from_row(row) if row["id"] is not None else None
    return CiCaseRecord(
        case_name=row["case_name"],
        physical_topology=row["physical_topology"] or "",
        logic_topology=topology,
        owner=row["owner"] or "",
    )
