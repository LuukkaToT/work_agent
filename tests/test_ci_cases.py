"""core/ci_cases.py：按 case_path 精确查 CI 用例表。"""

from __future__ import annotations

import uuid

from work_agent.core.ci_cases import CiCaseRecord, lookup_ci_case


def _insert(
    pool,
    case_path: str,
    *,
    logic_env: str = "",
    logic_constraint: str = "",
    version: str = "",
) -> None:
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO ci_cases (case_path, logic_env, logic_constraint, version)
            VALUES (%s, %s, %s, %s)
            """,
            (case_path, logic_env, logic_constraint, version),
        )


def test_lookup_miss_returns_none(pg_env):
    assert lookup_ci_case("no-such-case-path") is None
    assert lookup_ci_case("") is None
    assert lookup_ci_case("   ") is None


def test_lookup_hit_returns_record(pg_pool):
    path = f"ci/{uuid.uuid4().hex[:10]}/HF_20B_PUSCH_001"
    try:
        _insert(
            pg_pool,
            path,
            logic_env="3BBL_86_1BBL86",
            logic_constraint="85+86",
            version="27B",
        )
        rec = lookup_ci_case(path)
        assert rec == CiCaseRecord(
            case_path=path,
            logic_env="3BBL_86_1BBL86",
            logic_constraint="85+86",
            version="27B",
        )
        assert rec.logical_env_complete is True
    finally:
        with pg_pool.connection() as conn:
            conn.execute("DELETE FROM ci_cases WHERE case_path=%s", (path,))


def test_lookup_strips_whitespace(pg_pool):
    path = f"ci/{uuid.uuid4().hex[:10]}/case_a"
    try:
        _insert(pg_pool, path, version="26A")
        rec = lookup_ci_case(f"  {path}  ")
        assert rec is not None
        assert rec.version == "26A"
    finally:
        with pg_pool.connection() as conn:
            conn.execute("DELETE FROM ci_cases WHERE case_path=%s", (path,))


def test_lookup_incomplete_logical_env(pg_pool):
    path = f"ci/{uuid.uuid4().hex[:10]}/case_b"
    try:
        _insert(pg_pool, path, logic_env="3BBL_86", logic_constraint="", version="27A")
        rec = lookup_ci_case(path)
        assert rec is not None
        assert rec.logical_env_complete is False
        assert rec.logic_env == "3BBL_86"
        assert rec.version == "27A"
    finally:
        with pg_pool.connection() as conn:
            conn.execute("DELETE FROM ci_cases WHERE case_path=%s", (path,))
