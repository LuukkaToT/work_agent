"""agent_loop.run_agent_loop：显式 ReAct 循环的核心行为单测（全 mock，不起真图/真模型）。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from work_agent.graph.helpers.agent_loop import ReActStep, run_agent_loop, trim_steps
from work_agent.graph.helpers.context_archive import ContextArchive
from work_agent.graph.helpers.progress import reset_progress_hook, set_progress_hook


class _ScriptedModel:
    """按顺序回放预设 AIMessage，模拟模型每轮决策；不真正调用 LLM。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = list(responses)
        self.bind_tools_calls = 0
        self.invoke_calls = 0
        self.received: list[list[BaseMessage]] = []

    def bind_tools(self, tools):  # noqa: ANN001
        self.bind_tools_calls += 1
        return self

    def invoke(self, messages):  # noqa: ANN001
        self.invoke_calls += 1
        self.received.append(list(messages))
        return self._responses.pop(0)


@tool
def echo(text: str) -> str:
    """把输入原样返回（测试用工具）。"""
    return f"echo:{text}"


@tool
def boom() -> str:
    """总是抛异常（测试工具错误分支）。"""
    raise RuntimeError("boom failed")


def assert_tool_pairing(messages: list[BaseMessage]) -> None:
    """
    校验 tool_call 与 ToolMessage 严格配对。

    这是端点的硬约束：孤儿 tool_call 或孤儿 ToolMessage 会让下一次 invoke
    直接 400，所以裁剪历史后必须仍然成立。
    """
    pending: list[str] = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            assert not pending, f"上一步 tool_call 缺少配对的 ToolMessage: {pending}"
            pending = [
                tc.get("id") for tc in (msg.tool_calls or []) if tc.get("id")
            ]
        elif isinstance(msg, ToolMessage):
            assert msg.tool_call_id in pending, f"孤儿 ToolMessage: {msg.tool_call_id}"
            pending.remove(msg.tool_call_id)
    assert not pending, f"结尾仍有未配对的 tool_call: {pending}"


def _step(step_id: str, *, text: str, obs: str) -> ReActStep:
    """造一个带一次工具调用的 step。"""
    call_id = f"{step_id}-call"
    return ReActStep(
        step_id=step_id,
        assistant_message=AIMessage(
            content=text,
            tool_calls=[{"id": call_id, "name": "echo", "args": {"text": "x"}}],
        ),
        tool_messages=[ToolMessage(content=obs, tool_call_id=call_id, name="echo")],
    )


def test_no_tool_calls_returns_immediately():
    model = _ScriptedModel([AIMessage(content="没有异常，直接结论")])
    result = run_agent_loop(
        model=model, tools=[echo], system="sys", user="user", max_steps=5
    )
    assert model.invoke_calls == 1
    assert isinstance(result.messages[0], SystemMessage)
    assert isinstance(result.messages[1], HumanMessage)
    assert result.messages[-1].content == "没有异常，直接结论"
    assert result.steps == 1


def test_single_tool_round_then_final_answer():
    call = AIMessage(
        content="", tool_calls=[{"id": "1", "name": "echo", "args": {"text": "hi"}}]
    )
    final = AIMessage(content="根据 echo 结果得出结论")
    model = _ScriptedModel([call, final])

    result = run_agent_loop(
        model=model, tools=[echo], system="sys", user="user", max_steps=5
    )

    tool_msgs = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == "echo:hi"
    assert tool_msgs[0].name == "echo"
    assert result.messages[-1] is final
    assert_tool_pairing(result.messages)


def test_unknown_tool_is_rejected_without_raising():
    call = AIMessage(
        content="", tool_calls=[{"id": "1", "name": "not_registered", "args": {}}]
    )
    final = AIMessage(content="收到拒绝提示后仍给结论")
    model = _ScriptedModel([call, final])

    result = run_agent_loop(
        model=model, tools=[echo], system="sys", user="user", max_steps=5
    )

    tool_msg = next(m for m in result.messages if isinstance(m, ToolMessage))
    assert "不在白名单内" in tool_msg.content


def test_tool_exception_is_captured_as_observation():
    call = AIMessage(content="", tool_calls=[{"id": "1", "name": "boom", "args": {}}])
    final = AIMessage(content="工具报错后仍给结论")
    model = _ScriptedModel([call, final])

    result = run_agent_loop(
        model=model, tools=[boom], system="sys", user="user", max_steps=5
    )

    tool_msg = next(m for m in result.messages if isinstance(m, ToolMessage))
    assert "[tool error] boom" in tool_msg.content


def test_observation_is_compressed_when_too_long():
    long_text = "x" * 5000
    call = AIMessage(
        content="",
        tool_calls=[{"id": "1", "name": "echo", "args": {"text": long_text}}],
    )
    final = AIMessage(content="结论")
    model = _ScriptedModel([call, final])

    result = run_agent_loop(
        model=model,
        tools=[echo],
        system="sys",
        user="user",
        max_steps=5,
        observation_max_chars=200,
    )

    tool_msg = next(m for m in result.messages if isinstance(m, ToolMessage))
    assert len(tool_msg.content) <= 200


def test_max_steps_reached_appends_stop_hint_and_forces_final_answer():
    # 每轮都还想调用工具，直到触顶：run_agent_loop 应在最后一轮插入停止提示，
    # 再用 model.invoke 强制问一次，拿到最终结论。
    call = AIMessage(
        content="", tool_calls=[{"id": "1", "name": "echo", "args": {"text": "again"}}]
    )
    model = _ScriptedModel([call, call, AIMessage(content="被迫给出的结论")])

    result = run_agent_loop(
        model=model, tools=[echo], system="sys", user="user", max_steps=2
    )

    stop_hints = [
        m
        for m in result.messages
        if isinstance(m, HumanMessage) and "禁止再调用任何工具" in m.content
    ]
    assert len(stop_hints) == 1
    assert result.messages[-1].content == "被迫给出的结论"
    assert model.invoke_calls == 3


def test_progress_reported_for_thinking_and_tool_calls():
    call = AIMessage(
        content="", tool_calls=[{"id": "1", "name": "echo", "args": {"text": "hi"}}]
    )
    final = AIMessage(content="结论")
    model = _ScriptedModel([call, final])

    seen: list[str] = []
    token = set_progress_hook(seen.append)
    try:
        run_agent_loop(model=model, tools=[echo], system="sys", user="user", max_steps=5)
    finally:
        reset_progress_hook(token)

    assert seen == ["status:thinking", "tool:echo", "status:thinking"]


def test_usage_accumulates_across_steps():
    call = AIMessage(
        content="",
        tool_calls=[{"id": "1", "name": "echo", "args": {"text": "hi"}}],
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    )
    final = AIMessage(
        content="结论",
        usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
    )
    model = _ScriptedModel([call, final])

    result = run_agent_loop(
        model=model, tools=[echo], system="sys", user="user", max_steps=5
    )

    assert result.usage.input_tokens == 17
    assert result.usage.output_tokens == 7
    assert result.usage.total_tokens == 24
    assert result.usage.calls == 2


def test_trim_steps_noop_when_under_budget():
    steps = [_step("step01", text="想查日志", obs="ok")]
    trimmed, changed = trim_steps(steps, max_chars=10_000)
    assert changed == 0
    assert trimmed[0] is steps[0]


def test_trim_steps_disabled_when_budget_zero():
    steps = [_step("step01", text="a", obs="y" * 5000)]
    trimmed, changed = trim_steps(steps, max_chars=0)
    assert changed == 0
    assert trimmed == steps


def test_trim_steps_compacts_oldest_and_keeps_pairing():
    steps = [
        _step("step01", text="第一轮", obs="ERROR old " + "a" * 900),
        _step("step02", text="第二轮", obs="ERROR mid " + "b" * 900),
        _step("step03", text="第三轮", obs="ERROR new " + "c" * 900),
    ]
    trimmed, changed = trim_steps(steps, max_chars=1200, summary_max_chars=120)

    assert changed >= 1
    # 最新那步保留原样（仍带配对的 ToolMessage）
    assert trimmed[-1].step_id == "step03"
    assert trimmed[-1].has_tool_calls
    # 被压缩的老步骤不再带 tool_calls，也不再带 ToolMessage
    compacted = [s for s in trimmed if not s.has_tool_calls]
    assert compacted
    for step in compacted:
        assert step.tool_messages == []
        assert "[历史步骤" in step.assistant_message.content

    flat = [m for s in trimmed for m in s.to_messages()]
    assert_tool_pairing(flat)
    assert sum(s.chars for s in trimmed) <= 1200


def test_history_trimming_shrinks_what_model_receives():
    long_obs = "ERROR boom " + "z" * 3000

    def call(i: int) -> AIMessage:
        return AIMessage(
            content=f"第{i}轮",
            tool_calls=[{"id": str(i), "name": "echo", "args": {"text": long_obs}}],
        )

    model = _ScriptedModel([call(1), call(2), call(3), AIMessage(content="结论")])
    result = run_agent_loop(
        model=model,
        tools=[echo],
        system="sys",
        user="user",
        max_steps=4,
        observation_max_chars=4000,
        history_max_chars=2000,
        step_summary_max_chars=150,
    )

    assert result.trimmed_steps >= 1
    # 每次真正发给模型的消息都必须满足配对不变量
    for sent in model.received:
        assert_tool_pairing(sent)
    # 最后一次发送的上下文受预算约束，远小于未裁剪时的三轮全文
    assert result.context_chars < 3 * len(long_obs)


def test_trim_steps_with_archive_stores_original_and_marks_reference(tmp_path):
    """压缩分支：原文落盘，压缩消息挂引用；read 能取回原始观察全文。"""
    archive = ContextArchive(tmp_path, run_id="t")
    steps = [
        _step("step01", text="第一轮", obs="ERROR old " + "a" * 900),
        _step("step02", text="第二轮", obs="ERROR new " + "b" * 900),
    ]
    trimmed, changed = trim_steps(
        steps, max_chars=1000, summary_max_chars=120, archive=archive
    )

    assert changed == 1
    assert len(archive.refs) == 1
    ref = archive.refs[0]
    assert ref.artifact_id.startswith("react_step01") or "step01" in ref.artifact_id

    compacted = next(s for s in trimmed if not s.has_tool_calls)
    assert f"artifact {ref.artifact_id}" in compacted.assistant_message.content
    # 原文可回读：包含压缩摘要里已经看不到的完整观察
    restored = archive.read(ref.artifact_id)
    assert "[观察 echo]" in restored
    assert ("a" * 900) in restored

    flat = [m for s in trimmed for m in s.to_messages()]
    assert_tool_pairing(flat)


def test_trim_steps_drop_branch_leaves_stub_with_artifact(tmp_path):
    """整步丢弃分支：留一条 stub AIMessage 指向 artifact，且不带 tool_calls。"""
    archive = ContextArchive(tmp_path, run_id="t")
    steps = [
        _step("step01", text="最早一轮", obs="ERROR first " + "x" * 2000),
        _step("step02", text="第二轮", obs="y" * 1200),
        _step("step03", text="第三轮", obs="z" * 1200),
        _step("step04", text="第四轮", obs="w" * 1200),
    ]
    trimmed, changed = trim_steps(
        steps, max_chars=2600, summary_max_chars=60, archive=archive
    )

    stubs = [
        s
        for s in trimmed
        if not s.has_tool_calls and "已整体归档" in s.assistant_message.content
    ]
    assert changed >= 2
    assert stubs, "连摘要都塞不下的 step 必须留下归档占位"
    for s in stubs:
        assert s.tool_messages == []
        assert "artifact" in s.assistant_message.content
        # stub 引用的 artifact 真的能回读出该 step 的观察
        aid = s.assistant_message.content.split("artifact ")[-1].rstrip("]")
        assert _step_obs_marker(steps, s.step_id) in archive.read(aid)

    flat = [m for s in trimmed for m in s.to_messages()]
    assert_tool_pairing(flat)


def _step_obs_marker(steps: list[ReActStep], step_id: str) -> str:
    """取指定 step 观察里独有的长串片段，用于验证归档原文。"""
    step = next(s for s in steps if s.step_id == step_id)
    obs = step.tool_messages[0].content
    return obs[-50:]


def test_compact_without_archive_keeps_legacy_output():
    """无 archive 时 compact 输出不带任何归档引用（回归保护）。"""
    step = _step("step01", text="第一轮", obs="ERROR " + "a" * 900)
    compacted = step.compact(max_chars=120)
    assert "artifact" not in compacted.assistant_message.content
    assert "已归档" not in compacted.assistant_message.content


def test_run_agent_loop_archive_is_idempotent_across_compose_rounds(tmp_path):
    """compose() 每轮都会重算裁剪：同一 step 多轮触发只落盘一次。"""
    long_obs = "ERROR boom " + "q" * 3000

    def call(i: int) -> AIMessage:
        return AIMessage(
            content=f"第{i}轮",
            tool_calls=[{"id": str(i), "name": "echo", "args": {"text": long_obs}}],
        )

    model = _ScriptedModel([call(1), call(2), call(3), AIMessage(content="结论")])
    archive = ContextArchive(tmp_path, run_id="t")
    result = run_agent_loop(
        model=model,
        tools=[echo],
        system="sys",
        user="user",
        max_steps=4,
        observation_max_chars=4000,
        history_max_chars=2000,
        step_summary_max_chars=150,
        archive=archive,
    )

    assert result.trimmed_steps >= 1
    item_ids = [f"react_step{n:02d}" for n in (1, 2, 3)]
    stored = {r.artifact_id for r in archive.refs}
    # 每个 step 至多一条归档记录：store 按 item_id 去重
    assert len(stored) == len(archive.refs) <= 3
    for iid in item_ids:
        matches = [r.artifact_id for r in archive.refs if iid.replace("react_", "") in r.artifact_id]
        assert len(matches) <= 1
