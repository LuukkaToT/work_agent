"""
意图路由：单意图分类。
"""

from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from work_agent.core.llm import get_chat_model
from work_agent.graph.nodes.context import conversation_context
from work_agent.graph.state import TestFlowState


class RouteDecision(BaseModel):
    intent: Literal["analysis", "execute", "query", "start", "chat"] = Field(
        description=(
            "analysis=做测试分析; "
            "execute=创建/执行用例流水线; "
            "start=启动已创建但未跑的流水线; "
            "query=查某次流水线结果/进度; "
            "chat=普通问答"
        )
    )
    reason: str = Field(description="一句话说明分类理由")


def router(state: TestFlowState) -> dict:
    llm = get_chat_model(temperature=0).with_structured_output(RouteDecision)

    ctx = conversation_context(state, n=8)
    user_input = state.get("user_input") or ""

    human_parts = []
    if ctx:
        human_parts.append(ctx)
        human_parts.append("")
    human_parts.append("【本轮用户输入】")
    human_parts.append(user_input)

    decision: RouteDecision = llm.invoke(
        [
            SystemMessage(
                content=(
                    "你是测试助手的意图分类器。"
                    "根据本轮用户输入判断意图；若本轮是指代（如「再跑一遍」「换环境」"
                    "「刚才那次怎么样」「把刚才那几条启动起来」），"
                    "结合【历史摘要】和【最近对话】消解后再分类。"
                    "「只创建流水线」仍属 execute；"
                    "「启动刚才创建的」属 start。"
                    "不要执行任何操作。"
                )
            ),
            HumanMessage(content="\n".join(human_parts)),
        ]
    )
    return {
        "intent": decision.intent,
        "requirement": user_input,
        "audit": [
            {
                "step": "router",
                "intent": decision.intent,
                "reason": decision.reason,
            }
        ],
    }


def route_by_intent(state: TestFlowState) -> str:
    return state["intent"]
