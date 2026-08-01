"""append_audit reducer：追加语义 + 重置哨兵。"""

from work_agent.graph.state import RESET_AUDIT, append_audit


def test_append_by_default():
    old = [{"step": "intake"}]
    new = [{"step": "router"}]
    assert append_audit(old, new) == [{"step": "intake"}, {"step": "router"}]


def test_none_treated_as_empty():
    assert append_audit(None, None) == []
    assert append_audit(None, [{"step": "a"}]) == [{"step": "a"}]
    assert append_audit([{"step": "a"}], None) == [{"step": "a"}]


def test_reset_sentinel_drops_history():
    old = [{"step": "intake"}, {"step": "router"}, {"step": "respond"}]
    new = [{RESET_AUDIT: True}, {"step": "intake", "turn": 2}]
    merged = append_audit(old, new)
    # 历史被丢弃，哨兵本身也不留在结果里
    assert merged == [{"step": "intake", "turn": 2}]


def test_reset_sentinel_only():
    assert append_audit([{"step": "a"}], [{RESET_AUDIT: True}]) == []
