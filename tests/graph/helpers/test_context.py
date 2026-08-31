"""conversation_context / dialogue_text 拼装。"""

from langchain_core.messages import AIMessage, HumanMessage

from work_agent.graph.helpers.context import conversation_context, dialogue_text


def test_dialogue_text_recent_n():
    msgs = [
        HumanMessage(content="第一轮"),
        AIMessage(content="答1"),
        HumanMessage(content="第二轮"),
        AIMessage(content="答2"),
    ]
    text = dialogue_text(msgs, n=2)
    assert "第二轮" in text
    assert "答2" in text
    assert "第一轮" not in text


def test_conversation_context_empty():
    assert conversation_context({}) == ""
    assert conversation_context({"messages": [], "dialogue_summary": ""}) == ""


def test_conversation_context_summary_and_recent():
    state = {
        "dialogue_summary": "曾在 7.223.50.60 跑过 HF_20B_PUSCH_001",
        "messages": [
            HumanMessage(content="刚才那次怎么样了"),
            AIMessage(content="还在跑"),
        ],
    }
    text = conversation_context(state, n=8)
    assert "【历史摘要】" in text
    assert "HF_20B_PUSCH_001" in text
    assert "【最近对话】" in text
    assert "刚才那次怎么样了" in text


def test_conversation_context_summary_only():
    text = conversation_context(
        {"dialogue_summary": "只有摘要", "messages": []},
        n=8,
    )
    assert "【历史摘要】" in text
    assert "只有摘要" in text
    assert "【最近对话】" not in text
