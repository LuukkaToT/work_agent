"""memory 节点确定性部分：阈值、RemoveMessage、LLM 失败直通。"""

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from work_agent.graph.nodes import memory as memory_mod
from work_agent.graph.nodes.memory import memory


def _msgs(n: int) -> list:
    out = []
    for i in range(n):
        role = HumanMessage if i % 2 == 0 else AIMessage
        out.append(role(content=f"msg-{i}", id=f"id-{i}"))
    return out


def test_memory_skips_under_threshold():
    out = memory({"messages": _msgs(10), "dialogue_summary": ""})
    assert out["audit"][0]["skipped"] is True
    assert "dialogue_summary" not in out
    assert "messages" not in out


def test_memory_summarizes_and_removes_old(monkeypatch):
    monkeypatch.setattr(
        memory_mod, "invoke_text_fast", lambda *a, **k: "摘要：跑过 HF_A_001"
    )
    msgs = _msgs(14)  # > 12
    out = memory({"messages": msgs, "dialogue_summary": "旧摘要"})

    assert out["dialogue_summary"] == "摘要：跑过 HF_A_001"
    removals = out["messages"]
    assert all(isinstance(m, RemoveMessage) for m in removals)
    # 保留最近 8 条 → 删除前 6 条
    assert len(removals) == 6
    assert {m.id for m in removals} == {f"id-{i}" for i in range(6)}
    assert out["audit"][0]["removed"] == 6
    assert out["audit"][0]["kept"] == 8


def test_memory_llm_failure_passthrough(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(memory_mod, "invoke_text_fast", boom)
    out = memory({"messages": _msgs(14), "dialogue_summary": ""})
    assert out["audit"][0]["skipped"] is True
    assert "llm down" in out["audit"][0]["error"]
    assert "dialogue_summary" not in out
    assert "messages" not in out


def test_memory_empty_summary_skips(monkeypatch):
    monkeypatch.setattr(memory_mod, "invoke_text_fast", lambda *a, **k: "   ")
    out = memory({"messages": _msgs(14), "dialogue_summary": ""})
    assert out["audit"][0]["skipped"] is True
    assert out["audit"][0]["reason"] == "empty_summary"
