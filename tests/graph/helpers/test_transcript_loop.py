"""全量历史接线：截断前捕获、规则式组装、循环事件与回读。"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from work_agent.core.transcript import (
    MemoryTranscriptStore,
    Transcript,
    TranscriptConflict,
    TranscriptScope,
)
from work_agent.graph.helpers.agent_loop import run_agent_loop
from work_agent.graph.helpers.context_manager import ContextManager
from work_agent.graph.helpers.diagnose_tools import build_diagnose_tools
from work_agent.graph.helpers.transcript_recorder import load_recorded_messages
from work_agent.graph.helpers.truncate import CharBudget
from tests.graph.helpers.test_agent_loop import _ScriptedModel, assert_tool_pairing, boom, echo

MARKER = "UNIQUE_EVIDENCE_MIDDLE_TOKEN"


def _transcript(execution_id: str = "exec-loop") -> Transcript:
    return Transcript(
        MemoryTranscriptStore(),
        TranscriptScope("user-1", "task-1", "diagnose", execution_id),
    )


def _all_text(transcript: Transcript) -> str:
    return "\n".join(transcript.read(ref.artifact_id) for ref in transcript.refs)


def test_loop_records_start_and_end_without_tools():
    transcript = _transcript("no-tools")
    model = _ScriptedModel([AIMessage(content="直接结论")])
    result = run_agent_loop(
        model=model,
        tools=[echo],
        system="sys",
        user="user",
        max_steps=3,
        history_max_chars=2000,
        transcript=transcript,
    )
    assert result.messages[-1].content == "直接结论"
    assert transcript.get("loop/start") is not None
    assert transcript.get("loop/end") is not None
    assert load_recorded_messages(transcript, "react/001/input")
    assert result.input_event_ids == ["react/001/input"]


def test_loop_keeps_raw_tool_result_before_observation_limit():
    transcript = _transcript("obs-limit")
    body = ("x" * 2500) + MARKER + ("y" * 2500)
    model = _ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[{"id": "1", "name": "echo", "args": {"text": body}}],
            ),
            AIMessage(content="结论"),
        ]
    )
    result = run_agent_loop(
        model=model,
        tools=[echo],
        system="sys",
        user="user",
        max_steps=4,
        observation_max_chars=400,
        history_max_chars=3000,
        transcript=transcript,
    )
    raw = next(msg for msg in result.messages if isinstance(msg, ToolMessage))
    assert MARKER in raw.content
    sent_tool = next(msg for msg in model.received[1] if isinstance(msg, ToolMessage))
    assert len(sent_tool.content) <= 400
    assert MARKER not in sent_tool.content
    assert MARKER in _all_text(transcript)
    for sent in model.received:
        assert_tool_pairing(sent)


def test_diagnose_tool_persists_raw_before_first_budget_clip(monkeypatch):
    transcript = _transcript("tool-budget")
    blob = ("A" * 5000) + MARKER + ("B" * 5000)
    assert len(blob) > 8000

    class _HugeLog:
        def list_logs(self, pipeline_id: str) -> str:
            return "ok"

        def fetch_logs(self, pipeline_id: str, tail_lines: int = 200, component=None) -> str:
            return blob

        def grep_logs(self, *args, **kwargs) -> str:
            return MARKER

        def lookup_error_code(self, code: str) -> str:
            return "{}"

    monkeypatch.setattr(
        "work_agent.graph.helpers.diagnose_tools.get_log_tool",
        lambda **kwargs: _HugeLog(),
    )
    tools = {
        item.name: item
        for item in build_diagnose_tools(
            budget=CharBudget(limit=40_000),
            tool_result_max_chars=8000,
            transcript=transcript,
            archive=transcript,
        )
    }
    working = tools["fetch_logs"].invoke({"pipeline_id": "p1", "tail_lines": 400})

    assert MARKER in working
    assert MARKER in _all_text(transcript)
    clipped = CharBudget(limit=40_000).take(blob, max_chars=8000)
    assert MARKER not in clipped or len(clipped) < len(blob)


def test_loop_records_multiple_tool_calls_and_pairs_messages():
    transcript = _transcript("multi-tool")
    model = _ScriptedModel(
        [
            AIMessage(
                content="两步",
                tool_calls=[
                    {"id": "c1", "name": "echo", "args": {"text": "one"}},
                    {"id": "c2", "name": "echo", "args": {"text": "two"}},
                ],
            ),
            AIMessage(content="结论"),
        ]
    )
    result = run_agent_loop(
        model=model,
        tools=[echo],
        system="sys",
        user="user",
        max_steps=3,
        history_max_chars=4000,
        transcript=transcript,
    )
    assert transcript.get("react/001/tool/001/request") is not None
    assert transcript.get("react/001/tool/002/result") is not None
    for sent in model.received:
        assert_tool_pairing(sent)
    assert result.steps == 2


def test_max_steps_records_stop_hint_without_double_counting_tokens():
    transcript = _transcript("max-steps")
    call = AIMessage(
        content="",
        tool_calls=[{"id": "1", "name": "echo", "args": {"text": "again"}}],
        usage_metadata={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
    )
    final = AIMessage(
        content="被迫给出的结论",
        usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    )
    model = _ScriptedModel([call, call, final])
    result = run_agent_loop(
        model=model,
        tools=[echo],
        system="sys",
        user="user",
        max_steps=2,
        history_max_chars=4000,
        transcript=transcript,
    )
    assert transcript.get("react/final/input") is not None
    assert result.usage.calls == 3
    assert result.usage.input_tokens == 5 + 5 + 3
    assert "禁止再调用任何工具" in result.messages[-2].content


def test_unknown_tool_is_recorded_as_error_observation():
    transcript = _transcript("unknown-tool")
    model = _ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[{"id": "1", "name": "missing", "args": {}}],
            ),
            AIMessage(content="结论"),
        ]
    )
    result = run_agent_loop(
        model=model,
        tools=[echo],
        system="sys",
        user="user",
        max_steps=3,
        history_max_chars=2000,
        transcript=transcript,
    )
    event = transcript.get("react/001/tool/001/result")
    assert event.payload["status"] == "error"
    tool_msg = next(msg for msg in result.messages if isinstance(msg, ToolMessage))
    assert "不在白名单内" in tool_msg.content


def test_unexpected_tool_error_is_recorded_then_raised():
    transcript = _transcript("boom")
    model = _ScriptedModel(
        [AIMessage(content="", tool_calls=[{"id": "1", "name": "boom", "args": {}}])]
    )
    with pytest.raises(RuntimeError, match="boom failed"):
        run_agent_loop(
            model=model,
            tools=[boom],
            system="sys",
            user="user",
            max_steps=3,
            history_max_chars=2000,
            transcript=transcript,
        )
    assert transcript.get("loop/start") is not None
    assert transcript.get("react/001/tool/001/failure") is not None
    assert transcript.get("loop/end") is None


def test_reusing_same_transcript_is_conflict():
    transcript = _transcript("reuse")
    model = _ScriptedModel([AIMessage(content="一次")])
    run_agent_loop(
        model=model,
        tools=[echo],
        system="sys",
        user="user",
        max_steps=2,
        history_max_chars=1000,
        transcript=transcript,
    )
    with pytest.raises(TranscriptConflict):
        run_agent_loop(
            model=_ScriptedModel([AIMessage(content="二次")]),
            tools=[echo],
            system="sys",
            user="user",
            max_steps=2,
            history_max_chars=1000,
            transcript=transcript,
        )


def test_compose_messages_excerpts_but_archive_keeps_middle_evidence():
    transcript = _transcript("compose")
    raw = ("H" * 4000) + MARKER + ("T" * 4000)
    from work_agent.graph.helpers.agent_loop import ReActStep

    step = ReActStep(
        step_id="step01",
        assistant_message=AIMessage(
            content="查日志",
            tool_calls=[{"id": "c1", "name": "echo", "args": {}}],
        ),
        tool_messages=[ToolMessage(content=raw, tool_call_id="c1", name="echo")],
    )
    rendered = ContextManager(archive=transcript).compose_messages(
        prelude=[SystemMessage(content="sys"), HumanMessage(content="goal")],
        steps=[step],
        goal="goal",
        limit=8000,
        observation_max_chars=400,
    )
    assert_tool_pairing(rendered.messages)
    sent = next(msg for msg in rendered.messages if isinstance(msg, ToolMessage))
    assert len(sent.content) <= 400
    assert MARKER not in sent.content
    assert MARKER in _all_text(transcript)
    assert rendered.compressed_ids == ["step01"]


def test_fetch_archived_block_returns_uncut_original():
    transcript = _transcript("fetch-archive")
    raw = ("H" * 4000) + MARKER + ("T" * 4000)
    ref = transcript.put_text(raw)
    tools = {
        item.name: item
        for item in build_diagnose_tools(
            transcript=transcript,
            archive=transcript,
            budget=CharBudget(limit=40_000),
        )
    }
    out = tools["fetch_archived_block"].invoke({"artifact_id": ref.artifact_id})
    assert MARKER in out
    assert len(out) == len(raw)


def test_transcript_write_failure_is_not_swallowed():
    class _BoomStore(MemoryTranscriptStore):
        def _append(self, *args, **kwargs):
            raise OSError("disk full")

    transcript = Transcript(_BoomStore(), TranscriptScope("u", "t", "a", "fail-write"))
    with pytest.raises(OSError, match="disk full"):
        run_agent_loop(
            model=_ScriptedModel([AIMessage(content="结论")]),
            tools=[echo],
            system="sys",
            user="user",
            max_steps=2,
            history_max_chars=1000,
            transcript=transcript,
        )
