"""容量组确认和逻辑组网多选会展开执行计划。"""

from types import SimpleNamespace

from work_agent.graph.nodes.hitl import _expand_capacity_selections, ask_missing
from work_agent.graph.nodes.exec_flow import _plan_dict


CHOICES = [
    {
        "id": 1,
        "name": "BESA_NR_SDV_BBH_3BBL",
        "constraint": "H+SLG",
        "display_name": "BESA_NR_SDV_BBH_3BBL,H+SLG",
        "config": {"boards": []},
    },
    {
        "id": 2,
        "name": "BESA_NR_SDV_BBH_BBL_AI",
        "constraint": "H+G",
        "display_name": "BESA_NR_SDV_BBH_BBL_AI,H+G",
        "config": {"boards": []},
    },
]


def _capacity_plan():
    plan = _plan_dict(
        case_names=["CASE_A"],
        version="27B",
        env="",
        capacity_ids=["0x15109c", "0x10108d"],
    )
    plan["logic_candidates"] = list(CHOICES)
    plan["capacity_match"] = "exact"
    return plan


def test_exact_capacity_mapping_accepts_structured_multi_select(monkeypatch):
    payloads = []

    def fake_interrupt(payload):
        payloads.append(payload)
        return {"indices": [1, 2]}

    monkeypatch.setattr("work_agent.graph.nodes.hitl.interrupt", fake_interrupt)
    plans = _expand_capacity_selections([_capacity_plan()])

    assert payloads[0]["type"] == "pick_logic_topologies"
    assert payloads[0]["multiple"] is True
    assert len(plans) == 2
    assert {p["logic_topology"]["name"] for p in plans} == {
        "BESA_NR_SDV_BBH_3BBL",
        "BESA_NR_SDV_BBH_BBL_AI",
    }
    assert all(p["capacity_ids"] == ["0x10108d", "0x15109c"] for p in plans)


def test_fuzzy_match_confirms_group_before_topology(monkeypatch):
    plan = _plan_dict(
        case_names=["CASE_A"], version="27B", env="", capacity_ids=["0x1500e"]
    )
    plan["capacity_group_candidates"] = [
        {"capacity_ids": ["0x1500d"], "score": 0.92}
    ]
    answers = iter([{"index": 1}, {"indices": [1]}])
    payload_types = []

    def fake_interrupt(payload):
        payload_types.append(payload["type"])
        return next(answers)

    records = [
        SimpleNamespace(as_choice=lambda: dict(CHOICES[1]))
    ]
    monkeypatch.setattr("work_agent.graph.nodes.hitl.interrupt", fake_interrupt)
    monkeypatch.setattr(
        "work_agent.graph.nodes.hitl.lookup_capacity_topologies", lambda ids: records
    )

    plans = _expand_capacity_selections([plan])
    assert payload_types == ["pick_capacity_group", "pick_logic_topologies"]
    assert plans[0]["capacity_ids"] == ["0x1500d"]
    assert plans[0]["logic_topology"]["constraint"] == "H+G"


def test_capacity_selection_keeps_catalog_metadata_while_filling_version(monkeypatch):
    plan = _capacity_plan()
    plan["version"] = ""
    plan["missing"] = ["version", "env"]
    answers = iter([{"indices": [1]}, "27B"])
    monkeypatch.setattr(
        "work_agent.graph.nodes.hitl.interrupt", lambda payload: next(answers)
    )

    result = ask_missing({"exec_params": {"plans": [plan]}})

    selected = result["exec_params"]["plans"][0]
    assert selected["version"] == "27B"
    assert selected["missing"] == []
    assert selected["logic_topology"] == {
        "id": 1,
        "name": "BESA_NR_SDV_BBH_3BBL",
        "constraint": "H+SLG",
        "config": {"boards": []},
    }
