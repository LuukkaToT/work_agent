"""pipeline_ops.resolve_pipelines：验证 state["user_id"] 被转发进消解逻辑。"""

from __future__ import annotations

from work_agent.graph.nodes import pipeline_ops as mod
from work_agent.graph.nodes.pipeline_ops import resolve_pipelines


def test_resolve_pipelines_forwards_user_id(monkeypatch):
    seen: dict = {}

    def fake_pick(user_input, *, action="查询", user_id=None):
        seen["user_input"] = user_input
        seen["action"] = action
        seen["user_id"] = user_id
        return []

    monkeypatch.setattr(mod, "pick_records_for_action", fake_pick)

    resolve_pipelines(
        {"intent": "query", "user_input": "查一下上次的", "user_id": "z001"}
    )

    assert seen["user_id"] == "z001"
    assert seen["action"] == "查询"


def test_resolve_pipelines_defaults_user_id_empty_when_missing(monkeypatch):
    seen: dict = {}

    def fake_pick(user_input, *, action="查询", user_id=None):
        seen["user_id"] = user_id
        return []

    monkeypatch.setattr(mod, "pick_records_for_action", fake_pick)

    resolve_pipelines({"intent": "start", "user_input": "启动第一条"})

    assert seen["user_id"] == ""
