"""普通问答：轻量 LLM 直接回答，不走工具。"""

from langchain_core.messages import HumanMessage, SystemMessage

from work_agent.core.llm import get_chat_model
from work_agent.graph.state import TestFlowState


def quick_answer(state: TestFlowState) -> dict:
    llm = get_chat_model(temperature=0.3)
    resp = llm.invoke(
        [
            SystemMessage(
                content=(
                    "你是测试助手。简洁回答用户问题。"
                    "若问题其实是要执行用例或做测试分析，提醒用户换种说法。"
                )
            ),
            HumanMessage(content=state.get("user_input") or ""),
        ]
    )
    content = resp.content
    if isinstance(content, list):
        content = "".join(
            b.get("text", str(b)) if isinstance(b, dict) else str(b) for b in content
        )
    answer = str(content).strip()
    return {
        "summary": {
            "status": "ok",
            "branch": "chat",
            "answer": answer,
        },
        "audit": [{"step": "quick_answer", "chars": len(answer)}],
    }
