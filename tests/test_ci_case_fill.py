"""exec_params 优先级补参：口头 → CI 表 → version_space → missing。"""

from __future__ import annotations

from work_agent.core.ci_cases import CiCaseRecord
from work_agent.graph.nodes.exec_flow import (
    ExecPlanOut,
    _fill_plans_from_ci_and_config,
    _spoken_env_from_item,
)

CASE_A = "HF_20B_PUSCH_001"
CASE_B = "HF_20B_PUSCH_002"


def _rec(
    path: str,
    *,
    logic_env: str = "3BBL_86_1BBL86",
    logic_constraint: str = "85+86",
    version: str = "27A",
) -> CiCaseRecord:
    return CiCaseRecord(
        case_path=path,
        logic_env=logic_env,
        logic_constraint=logic_constraint,
        version=version,
    )


def _patch_lookups(monkeypatch, records: dict[str, CiCaseRecord], version_space=None):
    monkeypatch.setattr(
        "work_agent.graph.nodes.exec_flow.lookup_ci_case",
        lambda path: records.get(path),
    )
    monkeypatch.setattr(
        "work_agent.graph.nodes.exec_flow.get_version_space",
        lambda uid: version_space,
    )


def _plan(*, names, version="", env="", constraint=""):
    return {
        "case_names": list(names),
        "version": version,
        "env": env,
        "logic_constraint": constraint,
    }


def test_spoken_physical_wins_over_ci_and_version_space(monkeypatch):
    _patch_lookups(
        monkeypatch,
        {CASE_A: _rec(CASE_A, version="26A")},
        version_space="26B",
    )
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A], version="27B", env="7.223.50.60")],
        user_id="u1",
    )
    assert len(out) == 1
    assert out[0]["version"] == "27B"
    assert out[0]["env"] == "7.223.50.60"
    assert out[0]["env_kind"] == "physical"
    assert out[0]["logic_constraint"] == ""
    assert out[0]["missing"] == []


def test_spoken_complete_logical_wins_over_ci(monkeypatch):
    _patch_lookups(
        monkeypatch,
        {CASE_A: _rec(CASE_A, logic_env="OTHER", logic_constraint="99+00")},
    )
    out = _fill_plans_from_ci_and_config(
        [
            _plan(
                names=[CASE_A],
                version="27B",
                env="3BBL_86_1BBL86",
                constraint="85+86",
            )
        ],
        user_id="u1",
    )
    assert out[0]["env"] == "3BBL_86_1BBL86"
    assert out[0]["logic_constraint"] == "85+86"
    assert out[0]["missing"] == []


def test_ci_hit_fills_version_and_logical_env(monkeypatch):
    _patch_lookups(monkeypatch, {CASE_A: _rec(CASE_A)}, version_space="26B")
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A])],
        user_id="u1",
    )
    assert out[0]["version"] == "27A"
    assert out[0]["env"] == "3BBL_86_1BBL86"
    assert out[0]["logic_constraint"] == "85+86"
    assert out[0]["env_kind"] == "logical"
    assert out[0]["missing"] == []


def test_miss_uses_version_space_env_still_missing(monkeypatch):
    _patch_lookups(monkeypatch, {}, version_space="26A")
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A])],
        user_id="u1",
    )
    assert out[0]["version"] == "26A"
    assert out[0]["env"] == ""
    assert "env" in out[0]["missing"]
    assert "version" not in out[0]["missing"]


def test_miss_without_version_space_missing_env_and_version(monkeypatch):
    _patch_lookups(monkeypatch, {}, version_space=None)
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A])],
        user_id="u1",
    )
    assert "env" in out[0]["missing"]
    assert "version" in out[0]["missing"]


def test_different_ci_records_split_plans(monkeypatch):
    _patch_lookups(
        monkeypatch,
        {
            CASE_A: _rec(CASE_A, logic_env="ENV_A", version="27B"),
            CASE_B: _rec(CASE_B, logic_env="ENV_B", version="27B"),
        },
    )
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A, CASE_B])],
        user_id="u1",
    )
    assert len(out) == 2
    by_env = {p["env"]: p for p in out}
    assert by_env["ENV_A"]["case_names"] == [CASE_A]
    assert by_env["ENV_B"]["case_names"] == [CASE_B]


def test_illegal_spoken_version_not_overwritten_by_ci(monkeypatch):
    _patch_lookups(monkeypatch, {CASE_A: _rec(CASE_A, version="27B")})
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A], version="99Z", env="7.223.50.60")],
        user_id="u1",
    )
    assert out[0]["version"] == "99Z"
    assert "version" in out[0]["missing"]


def test_spoken_logical_env_takes_constraint_from_ci(monkeypatch):
    _patch_lookups(
        monkeypatch,
        {CASE_A: _rec(CASE_A, logic_constraint="85+86")},
    )
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A], version="27B", env="3BBL_86_1BBL86")],
        user_id="u1",
    )
    assert out[0]["env"] == "3BBL_86_1BBL86"
    assert out[0]["logic_constraint"] == "85+86"
    assert out[0]["missing"] == []


def test_version_space_does_not_fill_env(monkeypatch):
    _patch_lookups(monkeypatch, {}, version_space="27B")
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A], version="27B")],
        user_id="u1",
    )
    assert out[0]["version"] == "27B"
    assert out[0]["env"] == ""
    assert out[0]["missing"] == ["env"]


def test_sheet_rows_fill_per_case_path(monkeypatch):
    """表内缺 version/env 时，按该行用例路径走同一套 CI 补全。"""
    _patch_lookups(
        monkeypatch,
        {
            CASE_A: _rec(CASE_A, version="27B"),
            CASE_B: _rec(CASE_B, version="26A", logic_env="ENV_B"),
        },
    )
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A, CASE_B])],
        user_id="u1",
    )
    assert len(out) == 2
    by_name = {p["case_names"][0]: p for p in out}
    assert by_name[CASE_A]["version"] == "27B"
    assert by_name[CASE_B]["version"] == "26A"
    assert by_name[CASE_B]["env"] == "ENV_B"


def test_spoken_env_from_item_prefers_physical():
    item = ExecPlanOut(
        physical_env="7.223.50.60",
        logic_env="3BBL_86_1BBL86",
        logic_constraint="85+86",
    )
    assert _spoken_env_from_item(item) == ("7.223.50.60", "")
