"""router 的确定性部分：把 LLM 结构化输出写进 intent / requirement。不调真模型。"""

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


def test_router_writes_intent_and_requirement(monkeypatch):
    decision = RouteDecision(intent="chat", reason="闲聊")
    out = _run_router(monkeypatch, decision, user_input="你好")

    assert out["intent"] == "chat"
    assert out["requirement"] == "你好"
    assert out["audit"][0]["reason"] == "闲聊"
    assert "debug_mode" not in out


def test_router_execute_intent(monkeypatch):
    decision = RouteDecision(intent="execute", reason="要跑用例")
    out = _run_router(monkeypatch, decision, user_input="跑一下 HF_20B_PUSCH_001")
    assert out["intent"] == "execute"
