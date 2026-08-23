"""
显式 Agent Loop：模型 -> 工具调用 -> 观察 -> 再决策，直到给出结论或触顶 max_steps。

替代 langgraph.prebuilt.create_react_agent 这个黑盒子图：这里每一步都是显式代码，
好调试、好写单测（mock model.invoke 即可，不用起真图）。
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool

from work_agent.graph.helpers.context_budget import compress_observation
from work_agent.graph.helpers.progress import report_progress

_STOP_HINT = (
    "已达到最大步数，禁止再调用任何工具。"
    "请直接基于目前已知信息给出最终结论（不要再要求更多证据）。"
)


def run_agent_loop(
    *,
    model: BaseChatModel,
    tools: list[BaseTool],
    system: str,
    user: str,
    max_steps: int,
    observation_max_chars: int = 4000,
) -> list[BaseMessage]:
    """
    执行显式 ReAct 风格循环：bind_tools -> 按名执行白名单工具 -> 压缩观察 -> 再决策。

    参数:
        model: 已选好的 chat model（未 bind_tools，函数内部自己 bind）。
        tools: 白名单工具列表；不在此列表内的工具名一律拒绝执行，不抛异常。
        system: system prompt。
        user: 首轮 human 输入。
        max_steps: 最多允许的「模型决策」轮数（每轮 = 一次 model.invoke）。
        observation_max_chars: 单次工具结果写入 ToolMessage 前的压缩上限。

    返回:
        完整消息列表（含 system/human/AI/tool），可直接喂给
        extract_tool_trace / collect_compressed_observations。
    """
    tools_by_name = {t.name: t for t in tools}
    bound_model = model.bind_tools(tools)

    messages: list[BaseMessage] = [
        SystemMessage(content=system),
        HumanMessage(content=user),
    ]

    for step in range(max_steps):
        report_progress("status:thinking")
        ai_msg = bound_model.invoke(messages)
        messages.append(ai_msg)

        tool_calls = ai_msg.tool_calls or []
        if not tool_calls:
            return messages

        for tc in tool_calls:
            name = tc.get("name") or ""
            args = tc.get("args") or {}
            call_id = tc.get("id") or ""
            report_progress(f"tool:{name}")

            tool = tools_by_name.get(name)
            if tool is None:
                observation = f"[unknown tool] {name!r} 不在白名单内，拒绝执行"
            else:
                try:
                    raw = tool.invoke(args)
                except Exception as exc:  # noqa: BLE001
                    raw = f"[tool error] {name}: {exc}"
                observation = compress_observation(
                    str(raw), max_chars=observation_max_chars
                )

            messages.append(
                ToolMessage(content=observation, tool_call_id=call_id, name=name)
            )

        if step == max_steps - 1:
            messages.append(HumanMessage(content=_STOP_HINT))
            final_msg = model.invoke(messages)
            messages.append(final_msg)

    return messages