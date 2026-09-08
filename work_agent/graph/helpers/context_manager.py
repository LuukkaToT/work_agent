"""
上下文编排：把 Selector / Compressor / Archive 串起来。

本模块只负责编排，算法在 Selector、LLM 在 Compressor、写盘在 Archive。
处理链刻意让摘要后置，装得下就是零 LLM、零 IO：

    Normalize → Dedup → Rank → Budget
        ├─ 装得下 → Render
        └─ 装不下 → Compress → Re-budget → Archive discarded → Render
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from work_agent.core.usage import TokenUsage
from work_agent.graph.helpers.context_archive import Archive, ArchiveRef, reference_note
from work_agent.graph.helpers.context_compressor import ContextCompressor
from work_agent.graph.helpers.context_selector import (
    ContextItem,
    ImmutableBudgetExceeded,
    render_items,
    select,
)

if TYPE_CHECKING:
    from work_agent.graph.helpers.agent_loop import ReActStep

_DEFAULT_MAX_COMPRESS_ITEMS = 4
_DEFAULT_SUMMARY_MAX_CHARS = 600


@dataclass(frozen=True)
class RenderResult:
    """一次上下文渲染的产物与代价。"""

    text: str
    selected_ids: list[str]  # 进入最终上下文的 item_id，供 eval 记录
    context_chars: int  # 实际渲染出的字符数，A/B 的核心指标
    usage: TokenUsage = field(default_factory=TokenUsage)  # 压缩自身的 token 开销
    compressed_ids: list[str] = field(default_factory=list)
    degraded_ids: list[str] = field(default_factory=list)  # 摘要失败降级的块
    archived: list[ArchiveRef] = field(default_factory=list)
    llm_used: bool = False


@dataclass(frozen=True)
class MessageContext:
    """规则式消息投影；字符数包含固定消息、调用参数、摘录及归档引用。

    三类标识互斥，并各自维持输入顺序：selected_ids 表示原样保留的正文，
    compressed_ids 表示以摘录或整步摘要保留的正文，omitted_ids 表示未保留
    正文的步骤（可能只剩指针，也可能连指针都放不下）。步骤直接使用 step_id；
    任务状态使用 protected:<item_id>，取证缺口使用 gap:<step_id>:<call_id>。
    固定的 prelude/tail 没有另造标识，始终完整保留。
    """

    messages: list[BaseMessage]
    selected_ids: list[str]
    compressed_ids: list[str]
    omitted_ids: list[str]
    context_chars: int
    policy_version: str = "transcript_rules_v1"


class ContextManager:
    """按预算组装 working context，超预算时压缩 + 归档而不是静默丢弃。"""

    def __init__(
        self,
        *,
        compressor: ContextCompressor | None = None,
        archive: Archive | None = None,
        max_compress_items: int = _DEFAULT_MAX_COMPRESS_ITEMS,
        summary_max_chars: int = _DEFAULT_SUMMARY_MAX_CHARS,
    ) -> None:
        """
        参数:
            compressor: 摘要器；None 用默认 ``ContextCompressor``。
            archive: 归档器；None 表示不归档（被裁内容直接丢）。诊断链路应传，
                这样才有 external context 可回溯。
            max_compress_items: 单次最多压缩几块，防止 LLM 调用发散。
            summary_max_chars: 每块压缩后的字符上限。
        """
        self._compressor = compressor or ContextCompressor()
        self._archive = archive
        self._max_compress_items = max(1, max_compress_items)
        self._summary_max_chars = max(80, summary_max_chars)

    def compose_messages(
        self,
        *,
        prelude: list[BaseMessage],
        steps: list[ReActStep],
        tail: list[BaseMessage] | None = None,
        goal: str = "",
        limit: int = 20_000,
        observation_max_chars: int = 4000,
        protected_items: Iterable[ContextItem] = (),
    ) -> MessageContext:
        """从完整原文生成本轮模型输入，不修改历史，也不调用摘要模型。

        prelude 中的系统指令、用户目标和 tail 都属于固定消息，字符预算包含
        它们；goal 仅用于相关性排序，不再重复注入用户目标。调用方应把实际
        用户请求放入 prelude。protected_items 无论 pin、关键词或相关性如何，
        都作为必保留的任务状态；短文本逐字保留，长文本仅做确定性原文摘录。

        先校验完整调用/结果配对，再存储所有步骤、观察及任务状态的原文。
        每条长观察的可见摘录包含可回读引用，引用也占 observation_max_chars。
        固定部分和必保留部分装下之后，优先选择最近一步，再按目标词命中数、
        新近程度选择其他历史。预算不足时只能整步转为 AI 摘要或归档指针，
        不会留下孤儿 ToolMessage。最后按原始时间顺序展开，保持因果关系。

        与 render/旧 trim_steps 不同，limit<=0 不是禁用裁剪，而是配置错误。
        缺 archive、无效观察上限或不完整配对抛 ValueError；固定内容、必保留
        内容的最小表示或观察的引用装不下时抛 ImmutableBudgetExceeded。
        """
        # agent_loop 会调用本方法，因此运行时依赖必须放在方法内部。
        from work_agent.graph.helpers.agent_loop import ReActStep, _message_chars

        def cost(messages: list[BaseMessage]) -> int:
            return sum(_message_chars(message) for message in messages)

        if limit <= 0:
            raise ImmutableBudgetExceeded("消息总字符预算必须大于零")
        if observation_max_chars <= 0:
            raise ValueError("observation_max_chars 必须大于零")
        if self._archive is None:
            raise ValueError("规则式消息组装必须提供保存完整原文的 archive/transcript")
        tail = list(tail or [])
        fixed_cost = cost([*prelude, *tail])
        if fixed_cost > limit:
            raise ImmutableBudgetExceeded(
                f"固定 system/goal/tail 占 {fixed_cost} 字符，超过总预算 {limit}"
            )

        # 在任何归档写入之前拒绝缺结果、重复调用标识和跨步骤配对。
        _validate_message_pairs(prelude, location="prelude")
        _validate_message_pairs(tail, location="tail")
        identifiers: set[str] = set()

        def claim(identifier: str) -> None:
            if not identifier or identifier in identifiers:
                raise ValueError(f"消息上下文标识为空或重复：{identifier!r}")
            identifiers.add(identifier)

        for step in steps:
            if not isinstance(step, ReActStep) or not isinstance(step.assistant_message, AIMessage):
                raise ValueError("steps 必须由带 AIMessage 的完整 ReActStep 构成")
            claim(step.step_id)
            _validate_message_pairs(step.to_messages(), location=step.step_id)
        protected = list(protected_items)
        for item in protected:
            claim(f"protected:{item.item_id}")
        for step in steps:
            for message in step.tool_messages:
                if message.status == "error":
                    claim(f"gap:{step.step_id}:{message.tool_call_id}")

        # 必保留内容有常规表示和最小表示。预算吃紧时只缩短长摘录，绝不删除
        # 反证、未解决问题或取证失败；短任务状态没有摘要版本，必须逐字保留。
        required: list[tuple[str, HumanMessage, HumanMessage, str]] = []
        for item in protected:
            ref = _store_message_original(
                self._archive, key=f"protected:{item.item_id}", text=item.text,
                kind=item.kind, source=item.source,
            )
            prefix = f"[必保留任务状态 {item.item_id}；{item.kind}]\n"
            if len(item.text) <= _DEFAULT_SUMMARY_MAX_CHARS:
                message = HumanMessage(content=prefix + item.text)
                required.append((f"protected:{item.item_id}", message, message, "selected"))
            else:
                prefix += "以下是原文摘录，不是新增推理结论：\n"
                suffix = "\n" + reference_note(ref)
                preferred = HumanMessage(content=prefix + _rule_excerpt(item.text, 600) + suffix)
                minimum = HumanMessage(content=prefix + _rule_excerpt(item.text, 80) + suffix)
                required.append((f"protected:{item.item_id}", preferred, minimum, "compressed"))

        # 三种可见版本都从本轮原文构造，不复用上轮摘要，避免反复摘要造成漂移。
        candidates: list[tuple[list[BaseMessage], AIMessage, AIMessage, bool, int]] = []
        goal_tokens = _message_tokens(goal)
        for step in steps:
            original = json.dumps(
                {"step_id": step.step_id,
                 "messages": [message.model_dump() for message in step.to_messages()]},
                ensure_ascii=False, sort_keys=True, default=str,
            )
            step_ref = _store_message_original(
                self._archive, key=f"step:{step.step_id}", text=original,
                kind="react_step", source=step.step_id,
            )
            projected_assistant = step.assistant_message.model_copy(deep=True)
            shrunk_calls, args_changed = _shrink_tool_call_args(
                projected_assistant.tool_calls or []
            )
            if args_changed:
                projected_assistant.tool_calls = shrunk_calls
            projected: list[BaseMessage] = [projected_assistant]
            changed = args_changed
            raw_parts = [_message_text(step.assistant_message)]
            summary_parts = [f"[历史步骤 {step.step_id}；原文摘录，不是新增结论]"]
            if raw_parts[0]:
                summary_parts.append("当时文本：" + _rule_excerpt(raw_parts[0], 120))
            for message in step.tool_messages:
                raw = _message_text(message)
                name = message.name or next(
                    call["name"] for call in step.assistant_message.tool_calls
                    if call["id"] == message.tool_call_id
                )
                ref = _store_message_original(
                    self._archive, key=f"observation:{step.step_id}:{message.tool_call_id}",
                    text=raw, kind="tool_result", source=name,
                )
                copy = message.model_copy(deep=True)
                if len(raw) > observation_max_chars:
                    copy.content = _referenced_excerpt(raw, ref, observation_max_chars)
                    changed = True
                projected.append(copy)
                raw_parts.extend([name, raw])
                summary_parts.append(
                    f"观察 {name} / 调用 {message.tool_call_id}：" + _rule_excerpt(raw, 160)
                )
                if message.status == "error":
                    # 取证失败只能说明证据暂不可用，不能被解释成被测组件故障。
                    # 提示独立于历史选择，因此即便旧步骤正文完全省略也不会消失。
                    prefix = (
                        f"[取证缺口：工具执行失败，不代表组件故障] 工具={name}；"
                        f"步骤={step.step_id}；调用={message.tool_call_id}\n错误原文摘录："
                    )
                    suffix = "\n" + reference_note(ref)
                    preferred = HumanMessage(content=prefix + _rule_excerpt(raw, 240) + suffix)
                    minimum = HumanMessage(content=prefix + _rule_excerpt(raw, 80) + suffix)
                    required.append((
                        f"gap:{step.step_id}:{message.tool_call_id}", preferred, minimum,
                        "compressed" if len(raw) > 80 else "selected",
                    ))
            summary = AIMessage(content=(
                _rule_excerpt("\n".join(summary_parts), self._summary_max_chars)
                + "\n" + reference_note(step_ref)
            ))
            pointer = AIMessage(content=f"[历史步骤 {step.step_id} 正文省略]\n{reference_note(step_ref)}")
            relevance = len(goal_tokens & _message_tokens("\n".join(raw_parts)))
            candidates.append((projected, summary, pointer, changed, relevance))

        required_messages = [preferred for _, preferred, _, _ in required]
        if fixed_cost + cost(required_messages) > limit:
            required_messages = [minimum for _, _, minimum, _ in required]
        used = fixed_cost + cost(required_messages)
        if used > limit:
            raise ImmutableBudgetExceeded(
                f"固定消息、任务状态和取证缺口的最小表示占 {used} 字符，超过总预算 {limit}"
            )

        # 最新完整步骤优先，其余按简单词交集排序；相同分数优先较新的步骤。
        # 选择次序不等于输出次序，后面仍按原始下标展开，避免历史因果倒置。
        order = ([len(steps) - 1] if steps else []) + sorted(
            range(max(0, len(steps) - 1)), key=lambda i: (-candidates[i][4], -i)
        )
        chosen: dict[int, tuple[list[BaseMessage], str]] = {}
        for index in order:
            projected, summary, pointer, changed, _ = candidates[index]
            for messages, category in (
                (projected, "compressed" if changed else "selected"),
                ([summary], "compressed"),
                ([pointer], "omitted"),
            ):
                size = cost(messages)
                if used + size <= limit:
                    chosen[index] = (messages, category)
                    used += size
                    break
            else:
                # 连指针都放不下时不强塞 stub，省略信息只进入诊断元数据。
                chosen[index] = ([], "omitted")

        categories: dict[str, list[str]] = {"selected": [], "compressed": [], "omitted": []}
        for identifier, _, _, category in required:
            categories[category].append(identifier)
        visible: list[BaseMessage] = []
        for index, step in enumerate(steps):
            messages, category = chosen[index]
            visible.extend(messages)
            categories[category].append(step.step_id)
        messages = [
            *[message.model_copy(deep=True) for message in prelude],
            *[message.model_copy(deep=True) for message in required_messages],
            *visible,
            *[message.model_copy(deep=True) for message in tail],
        ]
        _validate_message_pairs(messages, location="最终模型输入")
        return MessageContext(
            messages=messages, selected_ids=categories["selected"],
            compressed_ids=categories["compressed"], omitted_ids=categories["omitted"],
            context_chars=cost(messages),
        )

    def render(
        self,
        items: Iterable[ContextItem],
        *,
        goal: str,
        limit: int,
    ) -> RenderResult:
        """
        选出并渲染 working context。

        参数:
            items: 候选上下文块。
            goal: 当前诊断目标，用于相关性打分。
            limit: 字符预算。

        返回:
            RenderResult；``selected_ids`` 与 ``text`` 严格对应。

        异常:
            ImmutableBudgetExceeded: immutable 块单独超预算（配置错误）。
        """
        first = select(items, goal=goal, limit=limit)
        if not first.over_budget:
            text = render_items(first.selected)
            return RenderResult(
                text=text,
                selected_ids=[i.item_id for i in first.selected],
                context_chars=len(text),
            )

        targets = list(first.needs_compression)
        room = max(0, self._max_compress_items - len(targets))
        # 被挤掉的里分数最高的几条，压成摘要塞回去，而不是整条丢掉
        rescued = first.dropped[:room]
        targets.extend(rescued)

        usage = TokenUsage()
        degraded: list[str] = []
        compressed: list[ContextItem] = []
        for item in targets:
            suffix = ""
            if self._archive is not None:
                suffix = reference_note(self._archive.store(item))
            outcome = self._compressor.compress(
                [item], max_chars_each=self._summary_max_chars, suffix=suffix
            )
            usage = usage + outcome.usage
            degraded.extend(outcome.degraded_ids)
            compressed.extend(outcome.items)

        target_ids = {i.item_id for i in targets}
        survivors = [i for i in first.selected if i.item_id not in target_ids]
        second = select(survivors + compressed, goal=goal, limit=limit)

        if self._archive is not None:
            # 没被救回来的、以及二次仍装不下的，全部归档：不静默丢信息
            self._archive.store_all(
                [i for i in first.dropped if i.item_id not in target_ids]
            )
            self._archive.store_all(second.dropped)

        text = render_items(second.selected)
        return RenderResult(
            text=text,
            selected_ids=[i.item_id for i in second.selected],
            context_chars=len(text),
            usage=usage,
            compressed_ids=[i.item_id for i in compressed],
            degraded_ids=degraded,
            archived=self._archive.refs if self._archive is not None else [],
            llm_used=usage.calls > 0,
        )


def _message_text(message: BaseMessage) -> str:
    """与循环的字符计数采用相同文本展开方式，兼容分块 content。"""
    from work_agent.graph.helpers.agent_loop import _content_text

    return _content_text(message)


def _rule_excerpt(text: str, max_chars: int) -> str:
    """只摘取原文头尾，不按 ERROR 筛选，避免正常日志及反证被关键词规则吞掉。"""
    if len(text) <= max_chars:
        return text
    marker = "\n…[原文中段省略]…\n"
    if max_chars < len(marker):
        return text[:max_chars]
    available = max_chars - len(marker)
    head = (available + 1) // 2
    tail = available - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _referenced_excerpt(text: str, ref: ArchiveRef, limit: int) -> str:
    """引用不可截断；单观察上限必须至少容纳引用和有意义的短摘录。"""
    suffix = "\n" + reference_note(ref)
    available = limit - len(suffix)
    if available < 32:
        raise ImmutableBudgetExceeded(
            f"观察预算 {limit} 无法容纳完整归档引用及至少 32 字符的原文摘录"
        )
    return _rule_excerpt(text, available) + suffix


def _store_message_original(
    archive: Archive, *, key: str, text: str, kind: str, source: str,
) -> ArchiveRef:
    """归档键含内容摘要：重复组装幂等，任务状态更新也不会误读同名旧原文。

    仅依赖 Archive 协议，既适用于全量 Transcript，也适用于测试内存归档。
    内容哈希只用作稳定标识，不参与相关性，也不改变原文。
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return archive.store(ContextItem(
        item_id=f"message:{key}:{digest}", kind=kind, source=source,
        text=text, priority=4,
    ))


def _message_tokens(text: str) -> set[str]:
    """确定性分词：英文标识按词，连续中文按相邻双字；不依赖检索 SDK。"""
    tokens: set[str] = set()
    for word in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.casefold()):
        if "\u4e00" <= word[0] <= "\u9fff" and len(word) > 1:
            tokens.update(word[i:i + 2] for i in range(len(word) - 1))
        else:
            tokens.add(word)
    return tokens


def _shrink_tool_call_args(tool_calls: list) -> tuple[list[dict], bool]:
    """投影时去掉超长调用参数；完整参数已随步骤原文归档。

    模型仍能看到工具名和调用 ID，以便与结果配对。参数原文不进工作上下文，
    避免 fetch_logs 一类调用把整段日志再复制进 tool_calls。
    """
    shrunk: list[dict] = []
    changed = False
    from work_agent.graph.helpers.agent_loop import _tool_call_payload

    for call in tool_calls:
        payload = _tool_call_payload(call)
        encoded = json.dumps(payload.get("args") or {}, ensure_ascii=False, default=str)
        if len(encoded) > 200:
            payload["args"] = {"_omitted": True, "chars": len(encoded)}
            changed = True
        shrunk.append(payload)
    return shrunk, changed


def _validate_message_pairs(messages: list[BaseMessage], *, location: str) -> None:
    """一轮多工具必须全部返回，且调用与结果之间不能插入其他消息。

    调用标识只要求在同一轮内唯一；不同完整步骤复用标识仍可正确配对。
    工具结果可以按不同顺序返回，但名称（若填写）必须与对应调用一致。
    """
    pending: dict[str, str] = {}
    for message in messages:
        if isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                raise ValueError(f"{location} 存在孤儿或重复工具结果：{message.tool_call_id}")
            name = pending.pop(message.tool_call_id)
            if message.name and message.name != name:
                raise ValueError(f"{location} 调用与结果的工具名不匹配：{message.tool_call_id}")
        else:
            if pending:
                raise ValueError(f"{location} 缺少工具结果：{', '.join(pending)}")
            if isinstance(message, AIMessage):
                for call in message.tool_calls or []:
                    identifier, name = call.get("id"), call.get("name")
                    if not identifier or not name or identifier in pending:
                        raise ValueError(f"{location} 工具调用标识为空、重复或缺少名称")
                    pending[identifier] = name
    if pending:
        raise ValueError(f"{location} 缺少工具结果：{', '.join(pending)}")
