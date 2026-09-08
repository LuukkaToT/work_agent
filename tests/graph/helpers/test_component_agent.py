"""组件 Agent：范围锁定、超时缺失报告、原文进 transcript。"""

from __future__ import annotations

import time

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from work_agent.core.transcript import MemoryTranscriptStore, Transcript, TranscriptScope
from work_agent.graph.helpers.component_agent import (
    collect_evidence,
    run_component_investigation,
)
from work_agent.graph.helpers.diagnose_tools import (
    apply_tool_scope,
    build_scoped_diagnose_tools,
)
from work_agent.graph.helpers.diagnosis_models import InvestigationBudget, InvestigationTask, LogScope
from work_agent.tools.registry import get_pipeline_tool


def _task(*, component: str = "bbh", pipeline_id: str = "p1", **kwargs) -> InvestigationTask:
    payload = {
        "investigation_id": kwargs.pop("investigation_id", "inv-bbh-1"),
        "diagnosis_task_id": "task-1",
        "round_index": 1,
        "component": component,
        "question": kwargs.pop("question", "时钟是否失锁"),
        "pipeline_id": pipeline_id,
        "log_scope": LogScope(
            pipeline_id=pipeline_id,
            component=component,
            tail_lines=200,
            version_tag="27B",
        ),
    }
    payload.update(kwargs)
    return InvestigationTask.model_validate(payload)


def _transcript(execution_id: str = "inv-bbh-1") -> Transcript:
    return Transcript(
        MemoryTranscriptStore(),
        TranscriptScope("user-1", "task-1", "component-bbh", execution_id),
    )


def test_apply_tool_scope_rejects_other_pipeline_and_component():
    with pytest.raises(ValueError, match="越界 pipeline_id"):
        apply_tool_scope(
            "fetch_logs",
            {"pipeline_id": "other", "component": "bbh"},
            pipeline_id="p1",
            component="bbh",
        )
    with pytest.raises(ValueError, match="越界 component"):
        apply_tool_scope(
            "grep_logs",
            {"pipeline_id": "p1", "component": "comm", "pattern": "ERROR"},
            pipeline_id="p1",
            component="bbh",
        )


def test_scoped_tools_inject_locked_component_and_reject_cross_component():
    scenario = "bench06_bbh_clock_unlocked"
    pipe = get_pipeline_tool(scenario=scenario)
    handle = pipe.create(
        case_names=["NR_BENCH_006_PTP_UNLOCK"],
        version="27B",
        physical_env="7.223.50.65",
    )
    pipe.start(handle.pipeline_id)
    tools = {
        t.name: t
        for t in build_scoped_diagnose_tools(
            pipeline_id=handle.pipeline_id,
            component="bbh",
            scenario=scenario,
        )
    }
    logs = tools["fetch_logs"].invoke({"pipeline_id": handle.pipeline_id, "tail_lines": 40})
    assert "E-BBH-2101" in logs
    assert "E-BBL-3112" not in logs
    with pytest.raises(ValueError, match="越界 component"):
        tools["fetch_logs"].invoke(
            {
                "pipeline_id": handle.pipeline_id,
                "component": "bbl",
                "tail_lines": 40,
            }
        )


def test_timeout_returns_missing_report_without_raising():
    task = _task()
    task = task.model_copy(
        update={"budget": InvestigationBudget(timeout_seconds=0.2, max_steps=2)}
    )
    transcript = _transcript()

    def _slow_loop(**kwargs):  # noqa: ANN003
        time.sleep(1.5)
        raise AssertionError("超时后不应再使用循环结果")

    report, evidences, _usage, trace = run_component_investigation(
        task,
        scenario="bench06_bbh_clock_unlocked",
        transcript=transcript,
        loop_fn=_slow_loop,
        extract_model=object(),
        model=object(),
    )
    assert report.status == "timeout"
    assert report.missing
    assert evidences == []
    assert trace == []
    assert "超时" in report.findings[0]


def test_collect_evidence_puts_original_text_and_skips_failures():
    task = _task()
    transcript = _transcript("inv-ev-1")
    messages = [
        ToolMessage(content="ptp_state=UNLOCKED errorcode=E-BBH-2101", tool_call_id="1", name="grep_logs"),
        ToolMessage(
            content="[tool error] fetch_logs: boom",
            tool_call_id="2",
            name="fetch_logs",
            status="error",
        ),
        ToolMessage(content="旁证手册", tool_call_id="3", name="search_knowledge"),
    ]
    items = collect_evidence(task, messages, transcript)
    assert [item.source for item in items] == ["log", "knowledge"]
    original = transcript.read(items[0].artifact_id)
    assert "E-BBH-2101" in original
    assert items[0].file == "bbh.log"


class _ScriptedModel:
    def __init__(self, responses):
        self._responses = list(responses)

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        return self._responses.pop(0)


class _ExtractFail:
    def with_structured_output(self, *args, **kwargs):  # noqa: ANN002
        raise RuntimeError("抽取失败走降级")


def test_component_agent_records_transcript_and_falls_back_report():
    scenario = "bench06_bbh_clock_unlocked"
    pipe = get_pipeline_tool(scenario=scenario)
    handle = pipe.create(
        case_names=["NR_BENCH_006_PTP_UNLOCK"],
        version="27B",
        physical_env="7.223.50.65",
    )
    pipe.start(handle.pipeline_id)
    task = _task(pipeline_id=handle.pipeline_id, investigation_id="invbbh001")
    task.budget.max_steps = 2
    transcript = Transcript(
        MemoryTranscriptStore(),
        TranscriptScope("user-1", "task-1", "component-bbh", "invbbh001"),
    )
    model = _ScriptedModel(
        [
            AIMessage(
                content="检索时钟",
                tool_calls=[
                    {
                        "id": "1",
                        "name": "grep_logs",
                        "args": {
                            "pipeline_id": handle.pipeline_id,
                            "pattern": "E-BBH-2101",
                            "component": "bbh",
                        },
                    }
                ],
            ),
            AIMessage(content="BBH 时钟失锁"),
        ]
    )
    report, evidences, _usage, trace = run_component_investigation(
        task,
        scenario=scenario,
        transcript=transcript,
        model=model,
        extract_model=_ExtractFail(),
    )
    assert report.status == "ok"
    assert any("E-BBH-2101" in item.excerpt for item in evidences)
    assert transcript.get("loop/start") is not None
    assert transcript.refs
    assert any(item.get("name") == "grep_logs" for item in trace if item.get("type") == "call")
