"""
意图路由：单意图分类。
"""

from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from work_agent.core.llm import get_fast_model
from work_agent.graph.helpers.context import conversation_context
from work_agent.graph.state import TestFlowState


class RouteDecision(BaseModel):
    """LLM 结构化输出：本轮意图 + 一句话理由。"""

    intent: Literal[
        "analysis", "execute", "query", "start", "diagnose", "chat", "set_mode"
    ] = Field(
        description=(
            "analysis=做测试分析(需求/规格);"
            "execute=创建/执行用例流水线(含按Excel表执行);"
            "start=启动已创建但未跑的流水线;"
            "query=查某次流水线结果/进度;"
            "diagnose=拉日志并归因失败原因;"
            "set_mode=开启/关闭调测模式等个人偏好设置;"
            "chat=普通问答"
        )
    )
    reason: str = Field(description="一句话说明分类理由")
    debug_mode_target: bool | None = Field(
        default=None,
        description=(
            "仅当 intent=set_mode 时填写：True=开启调试模式，False=关闭调试模式；"
            "无法判断是开启还是关闭时，不要选 set_mode，改分类为 chat 并把这个字段留空"
        ),
    )


def router(state: TestFlowState) -> dict:
    """
    用 LLM 对本轮输入做单意图分类，写入 intent / requirement。

    参数:
        state: 读 ``user_input`` 与对话上下文。

    返回:
        ``intent``、``requirement``（等于本轮输入）及 audit。
    """
    llm = get_fast_model(temperature=0).with_structured_output(RouteDecision)

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
                    "「刚才那次怎么样」「把刚才那几条启动起来」「看看为啥失败」），"
                    "结合【历史摘要】和【最近对话】消解后再分类。"
                    "「只创建流水线」仍属 execute；"
                    "「用表格/excel 执行」属 execute；"
                    "「启动刚才创建的」属 start；"
                    "「查进度/怎么样了」属 query；"
                    "「看日志/为什么失败/归因」属 diagnose；"
                    "「开启/关闭调试模式」「打开 debug」「切换到调测模式」属 set_mode，"
                    "并把要开启还是关闭的判断结果填进 debug_mode_target；"
                    "无法确定开关方向时不要选 set_mode，退回 chat。"
                    "不要执行任何操作。"
                )
            ),
            HumanMessage(content="\n".join(human_parts)),
        ]
    )
    out: dict[str, Any] = {
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
    if decision.intent == "set_mode" and decision.debug_mode_target is not None:
        out["debug_mode"] = decision.debug_mode_target
    return out


def route_by_intent(state: TestFlowState) -> str:
    """
    条件边：按 state["intent"] 选择下游节点名。

    参数:
        state: 须已由 router 写入 intent。

    返回:
        边标签（analysis / execute / start / query / diagnose / chat）。
    """
    return state["intent"]
