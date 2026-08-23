"""
会话记忆节点：对话变长后做滚动摘要，裁掉窗口外旧消息。

接在 respond 之后：respond → memory → END。
阈值以下直通，零 LLM 开销；LLM 失败不影响本轮。
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage

from work_agent.core.config import get_settings
from work_agent.core.llm import invoke_text_fast
from work_agent.graph.helpers.context import dialogue_text
from work_agent.graph.state import TestFlowState

_SYSTEM = """你是对话摘要器。把给定的旧对话压缩成不超过 10 行的中文摘要。

硬性要求：
1. 用例名、pipeline_id、版本号、环境 IP、路径必须原文保留，一个字符都不要改。
2. 只保留对后续指代有用的事实（做过什么、跑过什么、结果如何），删掉寒暄。
3. 不要编造摘要里没有的信息。
4. 不要用 markdown 标题。"""


def memory(state: TestFlowState) -> dict:
    """
    对话过长时做滚动摘要，并用 RemoveMessage 裁掉窗口外旧消息。

    参数:
        state: 读 ``messages`` / ``dialogue_summary``。

    返回:
        未超阈值或失败时仅写 audit；成功时写 ``dialogue_summary`` 与删除旧消息。
    """
    profile = get_settings().profile
    summary_threshold = profile.memory_summary_threshold
    keep_recent = profile.memory_keep_recent

    messages = list(state.get("messages") or [])
    if len(messages) <= summary_threshold:
        return {
            "audit": [
                {
                    "step": "memory",
                    "skipped": True,
                    "reason": "under_threshold",
                    "message_count": len(messages),
                }
            ]
        }

    old = messages[:-keep_recent]
    existing = str(state.get("dialogue_summary") or "").strip()
    old_text = dialogue_text(old, n=len(old))

    human_parts = []
    if existing:
        human_parts.append("【已有摘要】")
        human_parts.append(existing)
        human_parts.append("")
    human_parts.append("【需要并入摘要的旧对话】")
    human_parts.append(old_text or "(空)")

    try:
        summary = invoke_text_fast(
            [
                SystemMessage(content=_SYSTEM),
                HumanMessage(content="\n".join(human_parts)),
            ],
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001 - 记忆失败不能中断主流程
        return {
            "audit": [
                {
                    "step": "memory",
                    "skipped": True,
                    "error": str(exc),
                    "message_count": len(messages),
                }
            ]
        }

    if not summary.strip():
        return {
            "audit": [
                {
                    "step": "memory",
                    "skipped": True,
                    "reason": "empty_summary",
                    "message_count": len(messages),
                }
            ]
        }

    removals: list[RemoveMessage] = []
    for msg in old:
        mid = getattr(msg, "id", None)
        if mid:
            removals.append(RemoveMessage(id=mid))

    return {
        "dialogue_summary": summary.strip(),
        "messages": removals,
        "audit": [
            {
                "step": "memory",
                "removed": len(removals),
                "kept": keep_recent,
                "summary_chars": len(summary.strip()),
            }
        ],
    }
