"""
把 messages / dialogue_summary 转成可读上下文，供 router / exec_params 注入。

不负责截断策略以外的业务逻辑；调用方决定 n 取多大。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "".join(parts).strip()
    return str(content or "").strip()


def _role_label(msg: Any) -> str:
    """Human / AI / System / Tool → 中文角色名，便于模型读。"""
    t = getattr(msg, "type", None) or getattr(msg, "role", None) or ""
    mapping = {
        "human": "用户",
        "user": "用户",
        "ai": "助手",
        "assistant": "助手",
        "system": "系统",
        "tool": "工具",
    }
    return mapping.get(str(t).lower(), str(t) or "消息")


def dialogue_text(messages: Sequence[Any] | None, *, n: int = 8) -> str:
    """
    取最近 n 条消息，格式化为：

        用户: ...
        助手: ...

    messages 为空时返回空串；调用方应自行决定空上下文时怎么写 prompt。
    """
    if not messages:
        return ""
    recent = list(messages)[-n:]
    lines: list[str] = []
    for msg in recent:
        text = _content_text(getattr(msg, "content", ""))
        if not text:
            continue
        lines.append(f"{_role_label(msg)}: {text}")
    return "\n".join(lines)


def conversation_context(state: Mapping[str, Any] | None, *, n: int = 8) -> str:
    """
    拼装 LLM 上下文：历史摘要（若有）+ 最近 n 条对话。

        【历史摘要】
        ...

        【最近对话】
        用户: ...
        助手: ...
    """
    state = state or {}
    parts: list[str] = []

    summary = str(state.get("dialogue_summary") or "").strip()
    if summary:
        parts.append("【历史摘要】")
        parts.append(summary)
        parts.append("")

    history = dialogue_text(state.get("messages"), n=n)
    if history:
        parts.append("【最近对话】")
        parts.append(history)

    return "\n".join(parts).strip()
