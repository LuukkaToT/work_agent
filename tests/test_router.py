"""
router 的确定性部分：intent=set_mode 时才把 debug_mode_target 回写进 debug_mode，
其余分支完全不碰这个字段。LLM 分类本身用 monkeypatch 固定返回值，不调真模型。
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from work_agent.graph.nodes.router import RouteDecision, router


class _FakeStructuredLLM:
    def __init__(self, decision: RouteDecision):
        self._decision = decision

    def invoke(self, messages):
        return self._decision


class _FakeChatModel:
    def __init__(self, decision: RouteDecision):
        self._decision = decision

    def with_structured_output(self, schema):
        return _FakeStructuredLLM(self._decision)


def _run_router(monkeypatch, decision: RouteDecision, user_input: str = "问题") -> dict:
    monkeypatch.setattr(
        "work_agent.graph.nodes.router.get_fast_model",
        lambda temperature=0: _FakeChatModel(decision),
    )
    return router(
        {"messages": [HumanMessage(content=user_input)], "user_input": user_input}
    )


def test_router_set_mode_writes_debug_mode_target(monkeypatch):
    decision = RouteDecision(
        intent="set_mode", reason="用户要开启调试模式", debug_mode_target=True
    )
    out = _run_router(monkeypatch, decision, user_input="开启调试模式")

    assert out["intent"] == "set_mode"
    assert out["debug_mode"] is True


def test_router_set_mode_can_target_false(monkeypatch):
    decision = RouteDecision(
        intent="set_mode", reason="用户要关闭调试模式", debug_mode_target=False
    )
    out = _run_router(monkeypatch, decision, user_input="关闭调试模式")

    assert out["intent"] == "set_mode"
    assert out["debug_mode"] is False


def test_router_set_mode_without_target_does_not_touch_debug_mode(monkeypatch):
    """无法判断开关方向：不写 debug_mode，让下游按 intake 已读到的当前值处理。"""
    decision = RouteDecision(intent="set_mode", reason="方向不明确", debug_mode_target=None)
    out = _run_router(monkeypatch, decision, user_input="帮我设置一下模式")

    assert out["intent"] == "set_mode"
    assert "debug_mode" not in out


def test_router_other_intents_do_not_touch_debug_mode(monkeypatch):
    decision = RouteDecision(intent="chat", reason="闲聊")
    out = _run_router(monkeypatch, decision, user_input="你好")

    assert out["intent"] == "chat"
    assert "debug_mode" not in out
