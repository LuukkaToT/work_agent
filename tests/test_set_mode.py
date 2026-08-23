"""set_mode 节点：持久化 router 判定的 debug_mode 目标值，产出确定性 summary。"""

from __future__ import annotations

from work_agent.graph.nodes.set_mode import set_mode


def test_set_mode_persists_target_and_reports_summary(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "work_agent.graph.nodes.set_mode.set_debug_mode",
        lambda uid, value: calls.append((uid, value)),
    )

    out = set_mode({"user_id": "z00888363", "debug_mode": True})

    assert calls == [("z00888363", True)]
    assert out["summary"] == {"status": "ok", "debug_mode": True}
    assert out["audit"] == [
        {"step": "set_mode", "user_id": "z00888363", "debug_mode": True}
    ]


def test_set_mode_can_persist_false(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "work_agent.graph.nodes.set_mode.set_debug_mode",
        lambda uid, value: calls.append((uid, value)),
    )

    out = set_mode({"user_id": "z00888363", "debug_mode": False})

    assert calls == [("z00888363", False)]
    assert out["summary"]["debug_mode"] is False


def test_set_mode_defaults_user_id_to_empty_string(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "work_agent.graph.nodes.set_mode.set_debug_mode",
        lambda uid, value: calls.append((uid, value)),
    )

    set_mode({"debug_mode": True})

    assert calls == [("", True)]
