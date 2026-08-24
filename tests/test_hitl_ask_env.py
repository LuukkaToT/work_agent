"""HITL ask_env：物理 IP 或完整逻辑组网。"""

from __future__ import annotations

from work_agent.graph.nodes.exec_flow import _plan_dict
from work_agent.graph.nodes.hitl import (
    _apply_env_reply,
    _parse_env_reply,
    ask_missing,
)


def test_parse_env_reply_physical_ip():
    assert _parse_env_reply("7.223.50.60") == ("7.223.50.60", "")


def test_parse_env_reply_logical_slash():
    assert _parse_env_reply("3BBL_86_1BBL86 / 85+86") == (
        "3BBL_86_1BBL86",
        "85+86",
    )


def test_parse_env_reply_logical_chinese_comma():
    assert _parse_env_reply("3BBL_86_1BBL86，85+86") == (
        "3BBL_86_1BBL86",
        "85+86",
    )


def test_parse_env_reply_json():
    raw = '{"logic_env": "3BBL_86_1BBL86", "logic_constraint": "85+86"}'
    assert _parse_env_reply(raw) == ("3BBL_86_1BBL86", "85+86")


def test_parse_env_reply_dict_physical():
    assert _parse_env_reply({"physical_env": "7.223.50.60"}) == ("7.223.50.60", "")


def test_parse_env_reply_single_logical_is_incomplete():
    assert _parse_env_reply("3BBL_86_1BBL86") == ("3BBL_86_1BBL86", "")


def test_apply_env_reply_plus_fills_existing_logical_constraint():
    plan = _plan_dict(
        case_names=["HF_20B_PUSCH_001"],
        version="27B",
        env="3BBL_86_1BBL86",
    )
    env, constraint = _apply_env_reply(plan, "85+86")
    assert env == "3BBL_86_1BBL86"
    assert constraint == "85+86"


def test_ask_missing_accepts_logical_pair(monkeypatch):
    captured: list[dict] = []

    def fake_interrupt(payload):
        captured.append(payload)
        return "3BBL_86_1BBL86 / 85+86"

    monkeypatch.setattr("work_agent.graph.nodes.hitl.interrupt", fake_interrupt)
    plan = _plan_dict(
        case_names=["HF_20B_PUSCH_001"],
        version="27B",
        env="",
    )
    out = ask_missing({"exec_params": {"plans": [plan]}})
    filled = out["exec_params"]["plans"][0]
    assert filled["missing"] == []
    assert filled["env"] == "3BBL_86_1BBL86"
    assert filled["logic_constraint"] == "85+86"
    assert filled["env_kind"] == "logical"
    assert captured[0]["type"] == "ask_env"
    assert "物理组网 IP" in captured[0]["message"]
    assert "完整逻辑组网" in captured[0]["message"]
    assert "只支持物理" not in captured[0]["message"]


def test_ask_missing_accepts_physical_ip(monkeypatch):
    monkeypatch.setattr(
        "work_agent.graph.nodes.hitl.interrupt",
        lambda payload: "7.223.50.60",
    )
    plan = _plan_dict(
        case_names=["HF_20B_PUSCH_001"],
        version="27B",
        env="3BBL_86_1BBL86",
    )
    out = ask_missing({"exec_params": {"plans": [plan]}})
    filled = out["exec_params"]["plans"][0]
    assert filled["env"] == "7.223.50.60"
    assert filled["env_kind"] == "physical"
    assert filled["logic_constraint"] == ""
    assert filled["missing"] == []


def test_ask_missing_constraint_only_keeps_logical_env(monkeypatch):
    monkeypatch.setattr(
        "work_agent.graph.nodes.hitl.interrupt",
        lambda payload: "85+86",
    )
    plan = _plan_dict(
        case_names=["HF_20B_PUSCH_001"],
        version="27B",
        env="3BBL_86_1BBL86",
    )
    out = ask_missing({"exec_params": {"plans": [plan]}})
    filled = out["exec_params"]["plans"][0]
    assert filled["env"] == "3BBL_86_1BBL86"
    assert filled["logic_constraint"] == "85+86"
    assert filled["missing"] == []
