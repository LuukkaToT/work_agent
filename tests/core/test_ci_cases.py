"""core/ci_cases.py：按用例名查询默认组网和负责人。"""

from __future__ import annotations

import uuid

from work_agent.core.ci_cases import lookup_ci_case


def _topology_id(pool, name="BESA_SDV_2BBH_1BBL", constraint="1G_2A") -> int:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM logic_topologies WHERE name=%s AND constraint_value=%s",
            (name, constraint),
        ).fetchone()
    assert row
    return int(row["id"])


def _insert(
    pool,
    case_name: str,
    *,
    physical_topology: str = "",
    logic_topology_id: int | None = None,
    owner: str = "",
) -> None:
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO ci_cases (case_name, physical_topology, logic_topology_id, owner)
            VALUES (%s, %s, %s, %s)
            """,
            (case_name, physical_topology, logic_topology_id, owner),
        )


def _delete(pool, case_name: str) -> None:
    with pool.connection() as conn:
        conn.execute("DELETE FROM ci_cases WHERE case_name=%s", (case_name,))


def test_lookup_miss_returns_none(pg_env):
    assert lookup_ci_case("no-such-case") is None
    assert lookup_ci_case("") is None
    assert lookup_ci_case("   ") is None


def test_lookup_hit_returns_complete_logic_topology(pg_pool):
    case_name = f"ci/{uuid.uuid4().hex[:10]}/HF_20B_PUSCH_001"
    try:
        _insert(
            pg_pool,
            case_name,
            physical_topology="7.223.50.60",
            logic_topology_id=_topology_id(pg_pool),
            owner="张三",
        )
        rec = lookup_ci_case(case_name)
        assert rec is not None
        assert rec.case_name == case_name
        assert rec.physical_topology == "7.223.50.60"
        assert rec.owner == "张三"
        assert rec.logic_topology is not None
        assert rec.logic_topology.display_name == "BESA_SDV_2BBH_1BBL,1G_2A"
        assert rec.logical_env_complete is True
        assert rec.version == ""
    finally:
        _delete(pg_pool, case_name)


def test_lookup_physical_only_and_strips_whitespace(pg_pool):
    case_name = f"ci/{uuid.uuid4().hex[:10]}/case_a"
    try:
        _insert(pg_pool, case_name, physical_topology="7.223.50.61", owner="李四")
        rec = lookup_ci_case(f"  {case_name}  ")
        assert rec is not None
        assert rec.logic_topology is None
        assert rec.physical_topology == "7.223.50.61"
        assert rec.owner == "李四"
    finally:
        _delete(pg_pool, case_name)
