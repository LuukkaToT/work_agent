"""agent_loop.run_agent_loop：显式 ReAct 循环的核心行为单测（全 mock，不起真图/真模型）。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from work_agent.graph.helpers.agent_loop import run_agent_loop
from work_agent.graph.helpers.progress import reset_progress_hook, set_progress_hook


class _ScriptedModel:
    """按顺序回放预设 AIMessage，模拟模型每轮决策；不真正调用 LLM。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = list(responses)
        self.bind_tools_calls = 0
        self.invoke_calls = 0

    def bind_tools(self, tools):  # noqa: ANN001
        self.bind_tools_calls += 1
        return self

    def invoke(self, messages):  # noqa: ANN001
        self.invoke_calls += 1
        return self._responses.pop(0)


@tool
def echo(text: str) -> str:
    """把输入原样返回（测试用工具）。"""
    return f"echo:{text}"


@tool
def boom() -> str:
    """总是抛异常（测试工具错误分支）。"""
    raise RuntimeError("boom failed")


def test_no_tool_calls_returns_immediately():
    model = _ScriptedModel([AIMessage(content="没有异常，直接结论")])
    messages = run_agent_loop(
        model=model, tools=[echo], system="sys", user="user", max_steps=5
    )
    assert model.invoke_calls == 1
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert messages[-1].content == "没有异常，直接结论"


def test_single_tool_round_then_final_answer():
    call = AIMessage(
        content="", tool_calls=[{"id": "1", "name": "echo", "args": {"text": "hi"}}]
    )
    final = AIMessage(content="根据 echo 结果得出结论")
    model = _ScriptedModel([call, final])

    messages = run_agent_loop(
        model=model, tools=[echo], system="sys", user="user", max_steps=5
    )

    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == "echo:hi"
    assert tool_msgs[0].name == "echo"
    assert messages[-1] is final


def test_unknown_tool_is_rejected_without_raising():
    call = AIMessage(
        content="", tool_calls=[{"id": "1", "name": "not_registered", "args": {}}]
    )
    final = AIMessage(content="收到拒绝提示后仍给结论")
    model = _ScriptedModel([call, final])

    messages = run_agent_loop(
        model=model, tools=[echo], system="sys", user="user", max_steps=5
    )

    tool_msg = next(m for m in messages if isinstance(m, ToolMessage))
    assert "不在白名单内" in tool_msg.content


def test_tool_exception_is_captured_as_observation():
    call = AIMessage(content="", tool_calls=[{"id": "1", "name": "boom", "args": {}}])
    final = AIMessage(content="工具报错后仍给结论")
    model = _ScriptedModel([call, final])

    messages = run_agent_loop(
        model=model, tools=[boom], system="sys", user="user", max_steps=5
    )

    tool_msg = next(m for m in messages if isinstance(m, ToolMessage))
    assert "[tool error] boom" in tool_msg.content


def test_observation_is_compressed_when_too_long():
    long_text = "x" * 5000
    call = AIMessage(
        content="",
        tool_calls=[{"id": "1", "name": "echo", "args": {"text": long_text}}],
    )
    final = AIMessage(content="结论")
    model = _ScriptedModel([call, final])

    messages = run_agent_loop(
        model=model,
        tools=[echo],
        system="sys",
        user="user",
        max_steps=5,
        observation_max_chars=200,
    )

    tool_msg = next(m for m in messages if isinstance(m, ToolMessage))
    assert len(tool_msg.content) <= 200


def test_max_steps_reached_appends_stop_hint_and_forces_final_answer():
    # 每轮都还想调用工具，直到触顶：run_agent_loop 应在最后一轮插入停止提示，
    # 再用 model.invoke 强制问一次，拿到最终结论。
    call = AIMessage(
        content="", tool_calls=[{"id": "1", "name": "echo", "args": {"text": "again"}}]
    )
    model = _ScriptedModel([call, call, AIMessage(content="被迫给出的结论")])

    messages = run_agent_loop(
        model=model, tools=[echo], system="sys", user="user", max_steps=2
    )

    stop_hints = [
        m
        for m in messages
        if isinstance(m, HumanMessage) and "禁止再调用任何工具" in m.content
    ]
    assert len(stop_hints) == 1
    assert messages[-1].content == "被迫给出的结论"
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
