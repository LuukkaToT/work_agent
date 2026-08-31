"""口头逻辑组网：目录校验失败则按特征给出 HITL 候选。"""

from __future__ import annotations

from types import SimpleNamespace

from work_agent.graph.helpers.logic_env import (
    TopologyUtteranceOut,
    apply_logic_catalog,
    pick_logic_candidate,
)
from work_agent.graph.nodes.exec_flow import _plan_dict


def _patch_catalog(monkeypatch, *, valid_pairs: set[tuple[str, str]], candidates=None):
    candidates = candidates or []

    def _lookup(env, constraint):
        env, constraint = (env or "").strip(), (constraint or "").strip()
        if (env, constraint) in valid_pairs:
            return SimpleNamespace(logic_env=env, logic_constraint=constraint)
        return None

    monkeypatch.setattr(
        "work_agent.graph.helpers.logic_env.lookup_logic_topology",
        _lookup,
    )
    monkeypatch.setattr(
        "work_agent.graph.helpers.logic_env.is_valid_logic_topology",
        lambda e, c: (e or "").strip() and (c or "").strip()
        and ((e.strip(), c.strip()) in valid_pairs),
    )
    monkeypatch.setattr(
        "work_agent.graph.helpers.logic_env.find_logic_topology_candidates",
        lambda **kwargs: [
            SimpleNamespace(as_choice=lambda c=c: dict(c)) for c in candidates
        ],
    )
    monkeypatch.setattr(
        "work_agent.graph.helpers.logic_env.parse_topology_utterance",
        lambda text: TopologyUtteranceOut(
            kind="logical",
            bbh_count=2,
            bbl_count=1,
            bbh_board="G",
            bbl_board="A",
            logic_env="BESA_SDV_2BBH_1BBL",
            logic_constraint="9Z_9Z",
        ),
    )


def test_catalog_accepts_valid_pair(monkeypatch):
    _patch_catalog(
        monkeypatch,
        valid_pairs={("BESA_SDV_2BBH_1BBL", "1G_2A")},
    )
    plan = _plan_dict(
        case_names=["HF_20B_PUSCH_001"],
        version="27B",
        env="BESA_SDV_2BBH_1BBL",
        logic_constraint="1G_2A",
    )
    out = apply_logic_catalog([plan], user_input="用 2BBH+1BBL G板")
    assert out[0]["missing"] == []
    assert out[0].get("logic_candidates") is None


def test_invalid_pair_attaches_candidates(monkeypatch):
    choices = [
        {
            "logic_env": "BESA_SDV_2BBH_1BBL",
            "logic_constraint": "1G_2A",
            "bbh_count": 2,
            "bbl_count": 1,
            "bbh_board": "G",
            "bbl_board": "A",
        },
        {
            "logic_env": "BESA_SDV_2BBH_1BBL",
            "logic_constraint": "2G_1A",
            "bbh_count": 2,
            "bbl_count": 1,
            "bbh_board": "G",
            "bbl_board": "A",
        },
    ]
    _patch_catalog(
        monkeypatch,
        valid_pairs={("BESA_SDV_2BBH_1BBL", "1G_2A")},
        candidates=choices,
    )
    plan = _plan_dict(
        case_names=["HF_20B_PUSCH_001"],
        version="27B",
        env="2BBH+1BBL",
        logic_constraint="G板A板",
    )
    out = apply_logic_catalog(
        [plan], user_input="2BBH+1BBL 的环境，BBH 用 G 板，BBL 用 A 板"
    )
    assert "env" in out[0]["missing"]
    assert out[0]["logic_candidates"] == choices


def test_physical_skips_catalog(monkeypatch):
    _patch_catalog(monkeypatch, valid_pairs=set())
    plan = _plan_dict(
        case_names=["HF_20B_PUSCH_001"],
        version="27B",
        env="7.223.142.119",
    )
    out = apply_logic_catalog([plan], user_input="在 7.223.142.119 跑")
    assert out[0]["env_kind"] == "physical"
    assert out[0]["missing"] == []


def test_pick_candidate_by_index():
    plan = {
        "logic_candidates": [
            {"logic_env": "BESA_SDV_2BBH_1BBL", "logic_constraint": "1G_2A"},
            {"logic_env": "BESA_SDV_2BBH_1BBL", "logic_constraint": "2G_1A"},
        ]
    }
    assert pick_logic_candidate(plan, "2") == (
        "BESA_SDV_2BBH_1BBL",
        "2G_1A",
    )
    assert pick_logic_candidate(plan, {"index": 1}) == (
        "BESA_SDV_2BBH_1BBL",
        "1G_2A",
    )
    assert pick_logic_candidate(plan, "7.223.50.60") is None
