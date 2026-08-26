"""
显式 Agent Loop：模型 -> 工具调用 -> 观察 -> 再决策，直到给出结论或触顶 max_steps。

替代 langgraph.prebuilt.create_react_agent 这个黑盒子图：这里每一步都是显式代码，
好调试、好写单测（mock model.invoke 即可，不用起真图）。

历史裁剪按 **ReActStep 为原子单元**，不能按单条 Message 裁。原因是硬约束而非
风格取舍：OpenAI 兼容端点要求每个 ``tool_calls[].id`` 都有配对的 ``tool``
消息，留下带 tool_call 的 AIMessage 却删掉对应 ToolMessage，下一次 invoke
直接 400；孤儿 ToolMessage 同样非法。压缩一个 step 时整对替换成一条不带
tool_calls 的消息，配对不变量就天然成立。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool

from work_agent.core.usage import TokenUsage, usage_from_message
from work_agent.graph.helpers.context_archive import ContextArchive, reference_note
from work_agent.graph.helpers.context_budget import (
    PRIORITY_RAW_TOOL,
    compress_observation,
)
from work_agent.graph.helpers.context_selector import PIN_NORMAL, ContextItem
from work_agent.graph.helpers.progress import report_progress

_STOP_HINT = (
    "已达到最大步数，禁止再调用任何工具。"
    "请直接基于目前已知信息给出最终结论（不要再要求更多证据）。"
)

_DEFAULT_STEP_SUMMARY_MAX_CHARS = 400


@dataclass
class ReActStep:
    """
    ReAct 的一个原子单元：一次模型决策 + 它触发的全部工具结果。

    ``assistant_message`` 带 tool_calls 时，``tool_messages`` 必须与之一一配对。
    裁剪历史时只能整个 step 一起处理。
    """

    step_id: str
    assistant_message: AIMessage
    tool_messages: list[ToolMessage] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        """本步是否请求了工具调用。"""
        return bool(getattr(self.assistant_message, "tool_calls", None))

    @property
    def tool_names(self) -> list[str]:
        """本步调用到的工具名（按调用顺序）。"""
        return [
            (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")) or ""
            for tc in getattr(self.assistant_message, "tool_calls", None) or []
        ]

    @property
    def chars(self) -> int:
        """本步渲染进上下文的字符数。"""
        return sum(_message_chars(m) for m in self.to_messages())

    def to_messages(self) -> list[BaseMessage]:
        """展开成消息序列；AIMessage 紧跟它配对的 ToolMessage。"""
        return [self.assistant_message, *self.tool_messages]

    def compact(self, *, max_chars: int, archive: ContextArchive | None = None) -> ReActStep:
        """
        压成一条不带 tool_calls 的消息。

        整对替换，所以不会留下孤儿 tool_call 或孤儿 ToolMessage。

        参数:
            max_chars: 观察摘录的字符上限。
            archive: 归档器；传入时本 step 原文先落盘，压缩消息末尾挂
                artifact 引用（按 step_id 幂等，compose 每轮重算不会重复写）。
                模型发现摘要缺细节时可用 ``fetch_archived_block`` 取回原文。

        返回:
            只含单条 AIMessage 的新 step（原对象不变）。
        """
        names = "、".join(n for n in self.tool_names if n) or "无"
        observations = "\n".join(
            f"{m.name or 'tool'}: {compress_observation(_content_text(m), max_chars=max_chars)}"
            for m in self.tool_messages
        )
        head = _content_text(self.assistant_message).strip()
        suffix = ""
        if archive is not None and (head or self.tool_messages):
            ref = archive.store(self._archive_item())
            suffix = f"\n{reference_note(ref)}"
        parts = [f"[历史步骤 {self.step_id}] 调用：{names}"]
        if head:
            parts.append(f"当时判断：{compress_observation(head, max_chars=max_chars)}")
        if observations:
            parts.append(f"观察：{observations}")
        return ReActStep(
            step_id=self.step_id,
            assistant_message=AIMessage(content="\n".join(parts) + suffix),
            tool_messages=[],
        )

    def _full_text(self) -> str:
        """本 step 原文（判断 + 各观察全文），供归档。"""
        lines: list[str] = []
        head = _content_text(self.assistant_message).strip()
        if head:
            lines.append(f"[当时判断]\n{head}")
        for m in self.tool_messages:
            lines.append(f"[观察 {m.name or 'tool'}]\n{_content_text(m)}")
        return "\n\n".join(lines)

    def _archive_item(self) -> ContextItem:
        """归档用的 ContextItem；item_id 绑定 step_id，保证 store 幂等。"""
        return ContextItem(
            item_id=f"react_{self.step_id}",
            kind="react_step",
            source=self.step_id,
            text=self._full_text(),
            priority=PRIORITY_RAW_TOOL,
            pin=PIN_NORMAL,
        )

    def stub_step(self, *, archive: ContextArchive) -> ReActStep:
        """
        整步丢弃时留下的占位：单条 AIMessage，只说原文去了哪个 artifact。

        不带 tool_calls，配对不变量不破坏；模型至少知道有东西被收走了。
        """
        ref = None
        item = self._archive_item()
        if item.text.strip():
            ref = archive.store(item)
        note = (
            f"[历史步骤 {self.step_id}] 已整体归档"
            + (f"，详见 artifact {ref.artifact_id}" if ref is not None else "")
        )
        return ReActStep(
            step_id=self.step_id,
            assistant_message=AIMessage(content=note),
            tool_messages=[],
        )


@dataclass
class AgentLoopResult:
    """一次 ReAct 循环的产出与代价。"""

    messages: list[BaseMessage]  # 完整历史（未裁剪），供 trace / 观察提取
    usage: TokenUsage  # 本轮全部模型调用的 token 累计
    steps: int  # 实际发生的模型决策轮数
    context_chars: int  # 最后一次实际发送给模型的上下文字符数
    trimmed_steps: int  # 被压缩或丢弃的 step 数


def trim_steps(
    steps: list[ReActStep],
    *,
    max_chars: int,
    summary_max_chars: int = _DEFAULT_STEP_SUMMARY_MAX_CHARS,
    archive: ContextArchive | None = None,
) -> tuple[list[ReActStep], int]:
    """
    按 step 粒度裁剪历史：新的留全文，旧的压缩，压完还超就整步丢。

    ``max_chars <= 0`` 表示不裁剪（保持历史行为）。

    参数:
        steps: 已发生的 step 列表，按时间升序。
        max_chars: 历史部分的字符预算。
        summary_max_chars: 压缩单步观察时的字符上限。
        archive: 归档器；传入时被压缩/丢弃的 step 原文落盘并挂 artifact
            引用（压缩）或 stub 占位（丢弃），模型可按引用回读。

    返回:
        ``(裁剪后的 step 列表, 被压缩或丢弃的 step 数)``。
    """
    if max_chars <= 0 or not steps:
        return list(steps), 0
    if sum(s.chars for s in steps) <= max_chars:
        return list(steps), 0

    kept: list[ReActStep] = []
    used = 0
    changed = 0
    # 从最新往回走：近处的证据留全文，远处的降级
    for step in reversed(steps):
        if used + step.chars <= max_chars:
            kept.append(step)
            used += step.chars
            continue
        compacted = step.compact(max_chars=summary_max_chars, archive=archive)
        changed += 1
        if used + compacted.chars <= max_chars:
            kept.append(compacted)
            used += compacted.chars
        elif archive is not None:
            # 连摘要都塞不下：整步丢成一条归档占位（AIMessage 与 ToolMessage 一起走）
            kept.append(step.stub_step(archive=archive))
            used += kept[-1].chars
    kept.reverse()
    return kept, changed


def run_agent_loop(
    *,
    model: BaseChatModel,
    tools: list[BaseTool],
    system: str,
    user: str,
    max_steps: int,
    observation_max_chars: int = 4000,
    history_max_chars: int = 0,
    step_summary_max_chars: int = _DEFAULT_STEP_SUMMARY_MAX_CHARS,
    archive: ContextArchive | None = None,
) -> AgentLoopResult:
    """
    执行显式 ReAct 风格循环：bind_tools -> 按名执行白名单工具 -> 压缩观察 -> 再决策。

    参数:
        model: 已选好的 chat model（未 bind_tools，函数内部自己 bind）。
        tools: 白名单工具列表；不在此列表内的工具名一律拒绝执行，不抛异常。
        system: system prompt。
        user: 首轮 human 输入。
        max_steps: 最多允许的「模型决策」轮数（每轮 = 一次 model.invoke）。
        observation_max_chars: 单次工具结果写入 ToolMessage 前的压缩上限。
        history_max_chars: 历史 step 的字符预算；``0`` 表示不裁剪历史。
        step_summary_max_chars: 压缩历史 step 时每条观察的字符上限。
        archive: 归档器；传入且触发历史裁剪时，被压/被丢的 step 原文落盘，
            压缩消息挂 artifact 引用，模型可用 ``fetch_archived_block`` 回读。

    返回:
        AgentLoopResult；``messages`` 是未裁剪的完整历史，可直接喂给
        extract_tool_trace / collect_compressed_observations。
    """
    tools_by_name = {t.name: t for t in tools}
    bound_model = model.bind_tools(tools)

    prelude: list[BaseMessage] = [
        SystemMessage(content=system),
        HumanMessage(content=user),
    ]
    steps: list[ReActStep] = []
    tail: list[BaseMessage] = []
    usage = TokenUsage()
    context_chars = 0
    trimmed_total = 0
    decisions = 0

    def compose() -> list[BaseMessage]:
        """拼出本次真正发给模型的消息（system/goal 恒定不裁）。"""
        nonlocal trimmed_total, context_chars
        visible, trimmed = trim_steps(
            steps,
            max_chars=history_max_chars,
            summary_max_chars=step_summary_max_chars,
            archive=archive,
        )
        trimmed_total = max(trimmed_total, trimmed)
        sent = [*prelude, *[m for s in visible for m in s.to_messages()], *tail]
        context_chars = sum(_message_chars(m) for m in sent)
        return sent

    for step_index in range(max_steps):
        report_progress("status:thinking")
        ai_msg = bound_model.invoke(compose())
        usage = usage + usage_from_message(ai_msg)
        decisions += 1

        tool_calls = ai_msg.tool_calls or []
        if not tool_calls:
            steps.append(
                ReActStep(step_id=f"step{step_index + 1:02d}", assistant_message=ai_msg)
            )
            return AgentLoopResult(
                messages=_flatten(prelude, steps, tail),
                usage=usage,
                steps=decisions,
                context_chars=context_chars,
                trimmed_steps=trimmed_total,
            )

        step = ReActStep(
            step_id=f"step{step_index + 1:02d}", assistant_message=ai_msg
        )
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

            step.tool_messages.append(
                ToolMessage(content=observation, tool_call_id=call_id, name=name)
            )
        steps.append(step)

        if step_index == max_steps - 1:
            tail.append(HumanMessage(content=_STOP_HINT))
            # 用未 bind_tools 的 model：触顶后不允许再产生 tool_calls
            final_msg = model.invoke(compose())
            usage = usage + usage_from_message(final_msg)
            decisions += 1
            tail.append(final_msg)

    return AgentLoopResult(
        messages=_flatten(prelude, steps, tail),
        usage=usage,
        steps=decisions,
        context_chars=context_chars,
        trimmed_steps=trimmed_total,
    )


def _flatten(
    prelude: list[BaseMessage],
    steps: list[ReActStep],
    tail: list[BaseMessage],
) -> list[BaseMessage]:
    """把 prelude + 全部 step + tail 展平成完整历史。"""
    return [*prelude, *[m for s in steps for m in s.to_messages()], *tail]


def _content_text(message: BaseMessage) -> str:
    """取消息文本内容（content 可能是分段 list）。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content
        ]
        return "".join(parts)
    return str(content)


def _message_chars(message: BaseMessage) -> int:
    """估算单条消息占用的上下文字符数（含 tool_calls 参数）。"""
    total = len(_content_text(message))
    calls = getattr(message, "tool_calls", None) or []
    if calls:
        try:
            total += len(json.dumps(calls, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            total += len(str(calls))
    return total
