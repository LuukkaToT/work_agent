"""Deadline：超时先 cancel，禁止晚到的成功 transcript。"""

from __future__ import annotations

import time

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from work_agent.core.investigation_journal import MemoryInvestigationJournal
from work_agent.core.transcript import MemoryTranscriptStore, Transcript, TranscriptScope
from work_agent.graph.helpers.agent_loop import execute_tool_call, run_agent_loop
from work_agent.graph.helpers.component_agent import run_component_investigation
from work_agent.graph.helpers.deadline import Deadline, DeadlineExceeded, invoke_with_deadline
from work_agent.graph.helpers.diagnosis_models import InvestigationBudget, InvestigationTask, LogScope
from tests.graph.helpers.test_agent_loop import echo


class _SlowModel:
    def __init__(self, delay: float, response: AIMessage | None = None) -> None:
        self.delay = delay
        self.invokes = 0
        self.response = response or AIMessage(content="晚到结论")

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages, **kwargs):  # noqa: ANN001
        self.invokes += 1
        time.sleep(self.delay)
        return self.response


@tool
def sleepy_echo(text: str) -> str:
    """睡眠后再回显，模拟卡住的工具。"""
    time.sleep(1.0)
    return f"late:{text}"


def _transcript(execution_id: str) -> Transcript:
    return Transcript(
        MemoryTranscriptStore(),
        TranscriptScope("user-1", "task-1", "component-bbh", execution_id),
    )


def _task(**kwargs) -> InvestigationTask:
    payload = {
        "investigation_id": kwargs.pop("investigation_id", "inv-deadline-1"),
        "diagnosis_task_id": "task-1",
        "round_index": 1,
        "component": "bbh",
        "question": "时钟是否失锁",
        "pipeline_id": "p1",
        "log_scope": LogScope(pipeline_id="p1", component="bbh", tail_lines=200),
        "budget": InvestigationBudget(timeout_seconds=0.25, max_steps=2, history_max_chars=2000),
    }
    payload.update(kwargs)
    return InvestigationTask.model_validate(payload)


def test_deadline_check_and_cancel():
    deadline = Deadline(0.05)
    deadline.check()
    deadline.cancel()
    with pytest.raises(DeadlineExceeded):
        deadline.check()
    assert deadline.remaining() == 0.0


def test_invoke_with_deadline_falls_back_without_timeout_kwarg():
    model = _SlowModel(0.0, AIMessage(content="ok"))
    deadline = Deadline(5)
    result = invoke_with_deadline(model, [], deadline)
    assert result.content == "ok"
    assert model.invokes == 1


def test_timeout_cancels_before_late_model_response_is_recorded():
    task = _task()
    transcript = _transcript("exec-timeout-model")
    model = _SlowModel(1.2)

    report, evidences, _usage, trace = run_component_investigation(
        task,
        transcript=transcript,
        model=model,
        extract_model=object(),
    )
    time.sleep(1.4)
    assert report.status == "timeout"
    assert evidences == []
    assert trace == []
    assert model.invokes == 1
    assert transcript.get("loop/end") is None
    assert transcript.get("react/001/response") is None


def test_timeout_does_not_record_late_tool_result():
    task = _task()
    transcript = _transcript("exec-timeout-tool")
    model = _SlowModel(
        0.0,
        AIMessage(
            content="调用工具",
            tool_calls=[{"id": "1", "name": "sleepy_echo", "args": {"text": "x"}}],
        ),
    )
    report, _evidences, _usage, _trace = run_component_investigation(
        task,
        transcript=transcript,
        model=model,
        extract_model=object(),
        loop_fn=lambda **kwargs: run_agent_loop(
            **{key: value for key, value in kwargs.items() if key != "tools"},
            tools=[sleepy_echo],
        ),
    )
    time.sleep(1.2)
    assert report.status == "timeout"
    assert transcript.get("react/001/tool/001/result") is None
    kinds = [event.kind for event in transcript.events(limit=100)]
    assert "tool_result" not in kinds
    assert "loop_end" not in kinds


def test_timeout_still_charges_started_tool_requests():
    journal = MemoryInvestigationJournal()
    journal.ensure_run(
        "run-charge",
        user_id="user-1",
        pipeline_id="p1",
        max_rounds=3,
        max_tool_calls=24,
    )
    started: list[int] = []

    def _charge() -> None:
        started.append(1)
        journal.add_used_tool_calls("run-charge", 1)

    task = _task()
    transcript = _transcript("exec-timeout-charge")
    model = _SlowModel(
        0.0,
        AIMessage(
            content="调用工具",
            tool_calls=[{"id": "1", "name": "sleepy_echo", "args": {"text": "x"}}],
        ),
    )
    report, _evidences, _usage, _trace = run_component_investigation(
        task,
        transcript=transcript,
        model=model,
        extract_model=object(),
        on_tool_start=_charge,
        loop_fn=lambda **kwargs: run_agent_loop(
            **{key: value for key, value in kwargs.items() if key != "tools"},
            tools=[sleepy_echo],
        ),
    )
    time.sleep(1.2)
    assert report.status == "timeout"
    assert started == [1]
    assert journal.get_run("run-charge").used_tool_calls == 1
    journal.persist_pending(
        investigation_id="inv-charge",
        run_id="run-charge",
        round_index=1,
        component="bbh",
        question="时钟是否失锁",
        log_scope={"pipeline_id": "p1", "component": "bbh", "tail_lines": 200},
    )
    running = journal.claim("inv-charge", timeout_seconds=10)
    assert journal.finish_succeeded("inv-charge", running.owner_token, "ref", tool_calls=5)
    assert journal.get_run("run-charge").used_tool_calls == 1


def test_unknown_tool_does_not_charge():
    hits: list[int] = []
    observation = execute_tool_call(
        {"id": "1", "name": "missing_tool", "args": {}},
        {},
        on_tool_start=lambda: hits.append(1),
    )
    assert hits == []
    assert observation.status == "error"


def test_loop_without_deadline_still_writes_end():
    transcript = _transcript("exec-no-deadline")
    model = _SlowModel(0.0, AIMessage(content="直接结论"))
    result = run_agent_loop(
        model=model,
        tools=[echo],
        system="sys",
        user="user",
        max_steps=2,
        history_max_chars=2000,
        transcript=transcript,
    )
    assert result.messages[-1].content == "直接结论"
    assert transcript.get("loop/end") is not None
