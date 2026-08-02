"""
意图路由：

1. router 节点：调用 LLM，把用户话分类成 intent，写回 state
2. route_by_intent：条件边函数，只负责「读 intent，返回下一跳的 key」

注意：路由函数本身不是节点，不会写 state；它的返回值必须能在
add_conditional_edges 的映射表里找到。

分类时带上最近对话，否则「再跑一遍」「换个组网」会被误判成 chat。
"""

from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from work_agent.core.llm import get_chat_model
from work_agent.graph.nodes.context import dialogue_text
from work_agent.graph.state import TestFlowState


class RouteDecision(BaseModel):
    """让 LLM 按这个 schema 输出，避免解析自由文本。"""

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
    # temperature=0：分类任务要稳定，少随机
    # with_structured_output：强制模型吐出 RouteDecision，而不是一段散文
    llm = get_chat_model(temperature=0).with_structured_output(RouteDecision)

    history = dialogue_text(state.get("messages"), n=8)
    user_input = state.get("user_input") or ""

    human_parts = []
    if history:
        human_parts.append("【最近对话】")
        human_parts.append(history)
        human_parts.append("")
    human_parts.append("【本轮用户输入】")
    human_parts.append(user_input)

    decision: RouteDecision = llm.invoke(
        [
            SystemMessage(
                content=(
                    "你是测试助手的意图分类器。"
                    "根据本轮用户输入判断意图；若本轮是指代（如「再跑一遍」「换 topo_b」"
                    "「刚才那次怎么样」），结合【最近对话】消解后再分类。"
                    "不要执行任何操作。"
                )
            ),
            HumanMessage(content="\n".join(human_parts)),
        ]
    )
    # 节点只 return 要更新的字段；LangGraph 负责合并进总 state
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
    """条件边用：返回值 = 映射表的 key。"""
    return state["intent"]
