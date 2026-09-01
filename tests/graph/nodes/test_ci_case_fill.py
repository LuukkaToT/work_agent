"""执行补参：显式参数优先，CI 逻辑组网优先、物理组网兜底。"""

from __future__ import annotations

from work_agent.core.ci_cases import CiCaseRecord
from work_agent.core.logic_topologies import LogicTopologyRecord
from work_agent.graph.nodes.exec_flow import (
    ExecPlanOut,
    _fill_plans_from_ci_and_config,
    _spoken_env_from_item,
)

CASE_A = "HF_20B_PUSCH_001"
CASE_B = "HF_20B_PUSCH_002"


def _topology(name: str, constraint: str = "85+86") -> LogicTopologyRecord:
    return LogicTopologyRecord(
        id=1,
        name=name,
        constraint=constraint,
        config={"boards": []},
    )


def _rec(
    case_name: str,
    *,
    logic_env: str = "3BBL_86_1BBL86",
    logic_constraint: str = "85+86",
    physical_topology: str = "",
) -> CiCaseRecord:
    topology = _topology(logic_env, logic_constraint) if logic_env else None
    return CiCaseRecord(
        case_name=case_name,
        physical_topology=physical_topology,
        logic_topology=topology,
        owner="张三",
    )


def _patch_lookups(monkeypatch, records: dict[str, CiCaseRecord], version_space=None):
    monkeypatch.setattr(
        "work_agent.graph.nodes.exec_flow.lookup_ci_case",
        lambda name: records.get(name),
    )
    monkeypatch.setattr(
        "work_agent.graph.nodes.exec_flow.get_version_space",
        lambda uid: version_space,
    )


def _plan(*, names, version="", env="", constraint="", capacity_ids=None):
    return {
        "case_names": list(names),
        "version": version,
        "env": env,
        "logic_constraint": constraint,
        "capacity_ids": list(capacity_ids or []),
    }


def test_spoken_physical_wins_over_ci_and_version_space(monkeypatch):
    _patch_lookups(monkeypatch, {CASE_A: _rec(CASE_A)}, version_space="26B")
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A], version="27B", env="7.223.50.60")],
        user_id="u1",
    )
    assert out[0]["version"] == "27B"
    assert out[0]["env"] == "7.223.50.60"
    assert out[0]["env_kind"] == "physical"
    assert out[0]["missing"] == []


def test_spoken_complete_logical_wins_over_ci(monkeypatch):
    _patch_lookups(monkeypatch, {CASE_A: _rec(CASE_A, logic_env="OTHER")})
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A], version="27B", env="DIRECT", constraint="D+C")],
        user_id="u1",
    )
    assert out[0]["logic_topology"] == {"name": "DIRECT", "constraint": "D+C"}


def test_ci_logic_topology_and_version_space_fill_defaults(monkeypatch):
    _patch_lookups(monkeypatch, {CASE_A: _rec(CASE_A)}, version_space="26B")
    out = _fill_plans_from_ci_and_config([_plan(names=[CASE_A])], user_id="u1")
    assert out[0]["version"] == "26B"
    assert out[0]["env"] == "3BBL_86_1BBL86"
    assert out[0]["logic_constraint"] == "85+86"
    assert out[0]["env_kind"] == "logical"
    assert out[0]["missing"] == []


def test_ci_logic_topology_wins_when_both_defaults_exist(monkeypatch):
    rec = _rec(CASE_A, physical_topology="7.223.50.60")
    _patch_lookups(monkeypatch, {CASE_A: rec}, version_space="27B")
    out = _fill_plans_from_ci_and_config([_plan(names=[CASE_A])], user_id="u1")
    assert out[0]["env_kind"] == "logical"
    assert out[0]["env"] == "3BBL_86_1BBL86"


def test_ci_physical_topology_is_fallback(monkeypatch):
    rec = _rec(CASE_A, logic_env="", physical_topology="7.223.50.62")
    _patch_lookups(monkeypatch, {CASE_A: rec}, version_space="27B")
    out = _fill_plans_from_ci_and_config([_plan(names=[CASE_A])], user_id="u1")
    assert out[0]["env_kind"] == "physical"
    assert out[0]["env"] == "7.223.50.62"


def test_capacity_ids_prevent_ci_environment_default(monkeypatch):
    _patch_lookups(monkeypatch, {CASE_A: _rec(CASE_A)}, version_space="27B")
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A], capacity_ids=["0x1500C"])], user_id="u1"
    )
    assert out[0]["capacity_ids"] == ["0x1500c"]
    assert out[0]["env"] == ""
    assert "env" in out[0]["missing"]


def test_miss_uses_version_space_env_still_missing(monkeypatch):
    _patch_lookups(monkeypatch, {}, version_space="26A")
    out = _fill_plans_from_ci_and_config([_plan(names=[CASE_A])], user_id="u1")
    assert out[0]["version"] == "26A"
    assert out[0]["missing"] == ["env"]


def test_miss_without_version_space_missing_env_and_version(monkeypatch):
    _patch_lookups(monkeypatch, {}, version_space=None)
    out = _fill_plans_from_ci_and_config([_plan(names=[CASE_A])], user_id="u1")
    assert set(out[0]["missing"]) == {"env", "version"}


def test_different_ci_topologies_split_plans(monkeypatch):
    _patch_lookups(
        monkeypatch,
        {CASE_A: _rec(CASE_A, logic_env="ENV_A"), CASE_B: _rec(CASE_B, logic_env="ENV_B")},
        version_space="27B",
    )
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A, CASE_B])], user_id="u1"
    )
    assert {plan["env"] for plan in out} == {"ENV_A", "ENV_B"}


def test_illegal_spoken_version_is_not_overwritten(monkeypatch):
    _patch_lookups(monkeypatch, {CASE_A: _rec(CASE_A)}, version_space="27B")
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A], version="99Z", env="7.223.50.60")], user_id="u1"
    )
    assert out[0]["version"] == "99Z"
    assert "version" in out[0]["missing"]


def test_spoken_logic_name_can_take_ci_constraint(monkeypatch):
    _patch_lookups(monkeypatch, {CASE_A: _rec(CASE_A)}, version_space="27B")
    out = _fill_plans_from_ci_and_config(
        [_plan(names=[CASE_A], env="3BBL_86_1BBL86")], user_id="u1"
    )
    assert out[0]["logic_constraint"] == "85+86"


def test_spoken_env_from_item_prefers_physical():
    item = ExecPlanOut(
        physical_env="7.223.50.60",
        logic_env="3BBL_86_1BBL86",
        logic_constraint="85+86",
    )
    assert _spoken_env_from_item(item) == ("7.223.50.60", "")
