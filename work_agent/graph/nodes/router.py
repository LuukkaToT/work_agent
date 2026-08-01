from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from work_agent.core.llm import get_chat_model
from work_agent.graph.state import TestFlowState


class RouteDecision(BaseModel):
    intent: Literal["analysis", "execute", "query", "chat"] = Field(
        description=(
            "analysis=做测试分析; "
            "execute=执行用例; "
            "query=查某次执行结果; "
            "chat=普通问答"
        )
    )
    reason: str = Field(description="一句话说明分类理由")


def router(state: TestFlowState) -> dict:
    """调用 LLM，把用户输入分类成四种 intent 之一。"""
    llm = get_chat_model(temperature=0).with_structured_output(RouteDecision)
    decision: RouteDecision = llm.invoke(
        [
            SystemMessage(
                content=(
                    "你是测试助手的意图分类器。"
                    "只根据用户输入判断意图，不要执行任何操作。"
                )
            ),
            HumanMessage(content=state["user_input"]),
        ]
    )
    return {
        "intent": decision.intent,
        "requirement": state["user_input"],
        "audit": [
            {
                "step": "router",
                "intent": decision.intent,
                "reason": decision.reason,
            }
        ],
    }


def route_by_intent(state: TestFlowState) -> str:
    """条件边用的路由函数：返回值必须是边映射里的 key。"""
    return state["intent"]