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
from typing import TYPE_CHECKING, Callable

from httpx import HTTPError

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
from work_agent.graph.helpers.context_archive import Archive, reference_note
from work_agent.graph.helpers.context_budget import (
    PRIORITY_RAW_TOOL,
    compress_observation,
)
from work_agent.graph.helpers.context_selector import PIN_NORMAL, ContextItem
from work_agent.graph.helpers.deadline import Deadline, DeadlineExceeded
from work_agent.graph.helpers.progress import report_progress

if TYPE_CHECKING:
    from work_agent.core.transcript import Transcript

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

    def compact(self, *, max_chars: int, archive: Archive | None = None) -> ReActStep:
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

    def stub_step(self, *, archive: Archive) -> ReActStep:
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
    prompt_chars_sum: int = 0  # 各轮 compose 后发给模型的字符之和，不是最后一轮快照
    input_event_ids: list[str] = field(default_factory=list)  # 每轮实际输入，可按事件回读


def trim_steps(
    steps: list[ReActStep],
    *,
    max_chars: int,
    summary_max_chars: int = _DEFAULT_STEP_SUMMARY_MAX_CHARS,
    archive: Archive | None = None,
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
    archive: Archive | None = None,
    transcript: Transcript | None = None,
    protected_items: list[ContextItem] | None = None,
    deadline: Deadline | None = None,
    on_tool_start: Callable[[], None] | None = None,
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
        transcript: 全量历史流；传入时工具结果先保存完整正文，再由 ContextManager
            组装副本。此模式的 history_max_chars 是整份消息的字符预算，包含固定
            指令与引用，但不是供应商 tokenizer 的 token 上限。工具工厂也必须
            开启 preserve_raw，否则此前被工具出口截断的部分无法凭空恢复。
        protected_items: 宿主提供的反证、未解决问题等任务状态，不能由相关性裁掉。
        deadline: 可选截止；传入时每次模型/工具调用前检查，超时后不再写成功结果。
        on_tool_start: 白名单工具即将 invoke 前调用；用于按发起记账，未知工具不触发。

    返回:
        AgentLoopResult；旧模式保持压缩 observation 的兼容行为，transcript 模式
        的 messages 保存完整工具结果。两者均可用于提取 trace，只有组装后的副本
        才发给模型。跨进程回读历史不意味着从本循环中断位置恢复执行。
    """
    if transcript is not None and (max_steps < 1 or history_max_chars <= 0):
        raise ValueError("全量历史模式要求正数的步骤上限与上下文预算")
    tools_by_name = {t.name: t for t in tools}

    prelude: list[BaseMessage] = [
        SystemMessage(content=system),
        HumanMessage(content=user),
    ]
    steps: list[ReActStep] = []
    tail: list[BaseMessage] = []
    usage = TokenUsage()
    context_chars = 0
    prompt_chars_sum = 0
    trimmed_total = 0
    decisions = 0
    input_event_ids: list[str] = []
    recorder = None
    manager = None
    context_manifest: dict = {}
    if transcript is not None:
        # 一次执行流只能启动一次。再次运行必须使用新的 execution_id，不能把旧
        # transcript 当成执行账本，看到响应后就自行跳过工具或重置预算。
        from work_agent.core.transcript import TranscriptConflict
        from work_agent.graph.helpers.context_manager import ContextManager
        from work_agent.graph.helpers.transcript_recorder import TranscriptRecorder

        if deadline is not None:
            deadline.check()
        if transcript.get("loop/start") is not None:
            raise TranscriptConflict("该历史流已经启动过，请用新的执行 ID 或从 checkpoint 恢复")
        recorder = TranscriptRecorder(transcript, deadline=deadline)
        schemas = [
            {"name": t.name, "description": t.description, "parameters": t.get_input_schema().model_json_schema()}
            for t in tools
        ]
        tools_ref = transcript.put_json(schemas)
        recorder.messages(
            "loop/start", "loop_start", prelude,
            metadata={
                "max_steps": max_steps,
                "context_max_chars": history_max_chars,
                "observation_max_chars": observation_max_chars,
                "tools_ref": tools_ref.artifact_id,
                "model_name": str(getattr(model, "model_name", getattr(model, "model", ""))),
                "policy_version": "transcript_rules_v1",
            },
        )
        manager = ContextManager(archive=transcript)

    bound_model = model.bind_tools(tools)

    def compose() -> list[BaseMessage]:
        """拼出本次真正发给模型的消息（system/goal 恒定不裁）。"""
        nonlocal trimmed_total, context_chars, context_manifest
        if manager is not None:
            rendered = manager.compose_messages(
                prelude=prelude, steps=steps, tail=tail, goal=user,
                limit=history_max_chars, observation_max_chars=observation_max_chars,
                protected_items=protected_items or [],
            )
            context_chars = rendered.context_chars
            trimmed_total = max(trimmed_total, len(rendered.compressed_ids) + len(rendered.omitted_ids))
            context_manifest = {
                "policy_version": rendered.policy_version,
                "selected_ids": rendered.selected_ids,
                "compressed_ids": rendered.compressed_ids,
                "omitted_ids": rendered.omitted_ids,
                "context_chars": context_chars,
                "context_max_chars": history_max_chars,
            }
            return rendered.messages
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

    def invoke(invocation_id: str, selected_model) -> AIMessage:
        """固定本次输入后再调用模型；记录失败不能被当成普通观察继续。

        输入清单与消息正文在调用前提交，响应在调用后提交，崩溃时可以区分
        「尚未发起」「发起后结果不确定」和「响应已收到」，但不自行推断重试权限。
        """
        nonlocal prompt_chars_sum
        if deadline is not None:
            deadline.check()
        sent = compose()
        prompt_chars_sum += context_chars
        if recorder is None:
            from work_agent.graph.helpers.deadline import invoke_with_deadline

            return invoke_with_deadline(selected_model, sent, deadline)
        input_event_ids.append(f"{invocation_id}/input")
        return recorder.invoke(invocation_id, selected_model, sent, metadata=context_manifest)

    def finish() -> AgentLoopResult:
        """只在最后一条响应已经保存后记录循环结束，正文不回写主图状态。"""
        if transcript is not None and (deadline is None or not deadline.cancelled()):
            transcript.record("loop/end", "loop_end", {"decisions": decisions, "usage": usage.as_dict()})
        return AgentLoopResult(
            messages=_flatten(prelude, steps, tail), usage=usage, steps=decisions,
            context_chars=context_chars, trimmed_steps=trimmed_total,
            prompt_chars_sum=prompt_chars_sum, input_event_ids=input_event_ids,
        )

    for step_index in range(max_steps):
        if deadline is not None:
            deadline.check()
        report_progress("status:thinking")
        invocation_id = f"react/{step_index + 1:03d}"
        ai_msg = invoke(invocation_id, bound_model)
        usage = usage + usage_from_message(ai_msg)
        decisions += 1

        tool_calls = ai_msg.tool_calls or []
        if not tool_calls:
            steps.append(
                ReActStep(step_id=f"step{step_index + 1:02d}", assistant_message=ai_msg)
            )
            return finish()

        step = ReActStep(
            step_id=f"step{step_index + 1:02d}", assistant_message=ai_msg
        )
        for tool_index, tc in enumerate(tool_calls, start=1):
            tool_event_id = f"{invocation_id}/tool/{tool_index:03d}"
            if transcript is not None:
                if deadline is not None:
                    deadline.check()
                request = _tool_call_payload(tc)
                request_ref = transcript.put_json(request)
                transcript.record(
                    f"{tool_event_id}/request", "tool_request",
                    {"step_id": step.step_id, "tool_call_id": request.get("id"),
                     "tool_name": request.get("name"), "request_ref": request_ref.artifact_id},
                )
            try:
                observation = execute_tool_call(
                    tc, tools_by_name,
                    observation_max_chars=None if transcript is not None else observation_max_chars,
                    capture_expected_errors=transcript is not None,
                    deadline=deadline,
                    on_tool_start=on_tool_start,
                )
            except DeadlineExceeded:
                raise
            except Exception as exc:
                if recorder is not None:
                    from work_agent.core.transcript import TranscriptError

                    if not isinstance(exc, TranscriptError):
                        recorder.failure(f"{tool_event_id}/failure", exc, stage="tool")
                raise
            if recorder is not None:
                if deadline is not None:
                    deadline.check()
                text_ref = transcript.put_text(_content_text(observation))
                recorder.messages(
                    f"{tool_event_id}/result", "tool_result", [observation],
                    metadata={"step_id": step.step_id, "tool_call_id": observation.tool_call_id,
                              "tool_name": observation.name, "status": observation.status,
                              "text_ref": text_ref.artifact_id},
                )
            step.tool_messages.append(observation)
        steps.append(step)

        if step_index == max_steps - 1:
            tail.append(HumanMessage(content=_STOP_HINT))
            # 用未 bind_tools 的 model：触顶后不允许再产生 tool_calls
            final_msg = invoke("react/final", model)
            usage = usage + usage_from_message(final_msg)
            decisions += 1
            tail.append(final_msg)

    return finish()


def execute_tool_call(
    call: dict,
    tools_by_name: dict[str, BaseTool],
    *,
    observation_max_chars: int | None = None,
    propagate_errors: bool = False,
    capture_expected_errors: bool = False,
    deadline: Deadline | None = None,
    on_tool_start: Callable[[], None] | None = None,
) -> ToolMessage:
    """执行一个白名单调用，区分可呈现的取证失败和必须上抛的运行时错误。

    参数:
        observation_max_chars: 旧模式的观察压缩上限；None 保存原始返回，供新模式
            先持久化，再在 ContextManager 中投影。
        propagate_errors: 持久化子节点可选择把全部异常上抛，由调度层决定恢复。
        capture_expected_errors: 新模式仅将网络、权限、参数、缺失数据等已知错误
            转成 status=error 的观察。程序异常和 transcript 写入失败必须上抛，
            不能把数据库不可用伪装成「查不到日志」。旧调用方维持原来的兼容行为。
        deadline: 可选截止；到期则上抛 DeadlineExceeded，不当成工具失败观察。
        on_tool_start: 即将执行白名单工具前调用一次；未知工具不扣费。
    """
    if deadline is not None:
        deadline.check()
    name = call.get("name") or ""
    report_progress(f"tool:{name}")
    tool = tools_by_name.get(name)
    status = "success"
    if tool is None:
        status = "error"
        observation = f"[unknown tool] {name!r} 不在白名单内，拒绝执行"
    else:
        if on_tool_start is not None:
            on_tool_start()
        try:
            raw = tool.invoke(call.get("args") or {})
        except DeadlineExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            if propagate_errors:
                raise
            if capture_expected_errors and not isinstance(
                exc, (OSError, HTTPError, ValueError, KeyError)
            ):
                raise
            status = "error"
            raw = f"[tool error] {name}: {exc}"
        observation = _content_text(raw) if isinstance(raw, ToolMessage) else str(raw)
        if observation_max_chars is not None:
            observation = compress_observation(observation, max_chars=observation_max_chars)
    return ToolMessage(content=observation, tool_call_id=call.get("id") or "", name=name, status=status)


def _tool_call_payload(call: object) -> dict:
    """把供应商相关的 tool_call 对象收成可归档 JSON，不含模型客户端本身。"""
    if isinstance(call, dict):
        args = call.get("args") or {}
        return {"id": call.get("id") or "", "name": call.get("name") or "", "args": args}
    args = getattr(call, "args", None) or {}
    return {
        "id": getattr(call, "id", "") or "",
        "name": getattr(call, "name", "") or "",
        "args": args,
    }


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
