"""普通问答：轻量 LLM 直接回答，不走工具。"""

from langchain_core.messages import HumanMessage, SystemMessage

from work_agent.core.llm import invoke_text
from work_agent.graph.state import TestFlowState


def quick_answer(state: TestFlowState) -> dict:
    """
    chat 意图：轻量 LLM 直接回答，结果写入 summary.answer。

    参数:
        state: 读 ``user_input``。

    返回:
        ``summary``（status/answer）与 audit。
    """
    answer = invoke_text(
        [
            SystemMessage(
                content=(
                    "你是测试助手。简洁回答用户问题。"
                    "若问题其实是要执行用例或做测试分析，提醒用户换种说法。"
                )
            ),
            HumanMessage(content=state.get("user_input") or ""),
        ],
        temperature=0.3,
    )
    return {
        "summary": {
            "status": "ok",
            "answer": answer,
        },
        "audit": [{"step": "quick_answer", "chars": len(answer)}],
    }
