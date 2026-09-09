"""run_diagnosis 的 legacy / managed 双策略接线（注入假模型，不调真 LLM）。"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

import work_agent.graph.nodes.error_analysis as ea
from work_agent.core.transcript import MemoryTranscriptStore, Transcript, TranscriptScope
from work_agent.graph.helpers.context_archive import ContextArchive
from work_agent.graph.helpers.context_compressor import ContextCompressor
from work_agent.graph.nodes.error_analysis import ErrorAnalysisOut, run_diagnosis

_PID = "eval-pipeline-1"
_BRIEF = [
    {
        "pipeline_id": _PID,
        "case_names": ["CaseA_235T_nmimo"],
        "version": "27B",
        "env": "7.223.50.60",
        "status": "failed",
    }
]


class _FakeReasoning:
    """ReAct 侧假模型：先重复取两次同样的日志，再 grep，最后给结论。"""

    def __init__(self) -> None:
        self.invoke_calls = 0
        self._script = [
            AIMessage(
                content="先看日志",
                tool_calls=[
                    {
                        "id": "1",
                        "name": "fetch_logs",
                        "args": {"pipeline_id": _PID, "tail_lines": 400},
                    }
                ],
                usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
            ),
            AIMessage(
                content="再确认一遍同一份日志",
                tool_calls=[
                    {
                        "id": "2",
                        "name": "fetch_logs",
                        "args": {"pipeline_id": _PID, "tail_lines": 400},
                    }
                ],
                usage_metadata={"input_tokens": 120, "output_tokens": 10, "total_tokens": 130},
            ),
            AIMessage(
                content="定位报错行",
                tool_calls=[
                    {
                        "id": "3",
                        "name": "grep_logs",
                        "args": {"pipeline_id": _PID, "pattern": "KeyError"},
                    }
                ],
                usage_metadata={"input_tokens": 140, "output_tokens": 10, "total_tokens": 150},
            ),
            AIMessage(
                content="用例脚本读取 antenna_map 时抛 KeyError，属于用例自身问题",
                usage_metadata={"input_tokens": 160, "output_tokens": 20, "total_tokens": 180},
            ),
        ]

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        self.invoke_calls += 1
        return self._script[min(self.invoke_calls - 1, len(self._script) - 1)]


class _FakeStructured:
    """with_structured_output(include_raw=True) 的返回形状。"""

    def __init__(self) -> None:
        self.received: list = []

    def invoke(self, messages):  # noqa: ANN001
        self.received = list(messages)
        return {
            "raw": AIMessage(
                content="",
                usage_metadata={
                    "input_tokens": 300,
                    "output_tokens": 40,
                    "total_tokens": 340,
                },
            ),
            "parsed": ErrorAnalysisOut(
                fail_kind="case",
                evidence="KeyError: 'antenna_map'",
                conclusion="用例脚本缺少 antenna_map 配置",
                suggestion="补齐用例参数后重跑",
                ruled_out=[],
            ),
            "parsing_error": None,
        }


class _FakeFast:
    """抽取用快模型；同时充当压缩器的模型。"""

    def __init__(self) -> None:
        self.structured = _FakeStructured()
        self.compress_calls = 0

    def with_structured_output(self, schema, include_raw: bool = False, method: str = ""):  # noqa: ANN001
        assert include_raw, "抽取必须用 include_raw=True，否则这次调用的 token 统计不到"
        return self.structured

    def invoke(self, messages):  # noqa: ANN001
        self.compress_calls += 1
        return AIMessage(
            content="压缩摘要",
            usage_metadata={"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
        )


def _install(monkeypatch) -> tuple[_FakeReasoning, _FakeFast]:
    reasoning = _FakeReasoning()
    fast = _FakeFast()
    monkeypatch.setattr(ea, "get_reasoning_model", lambda **kw: reasoning)
    monkeypatch.setattr(ea, "get_fast_model", lambda **kw: fast)
    return reasoning, fast


def _run(strategy: str, tmp_path, monkeypatch):
    _install(monkeypatch)
    fast_for_compress = _FakeFast()
    return run_diagnosis(
        pipelines=_BRIEF,
        user_input="CaseA_235T_nmimo 为什么报 KeyError？",
        context_strategy=strategy,  # type: ignore[arg-type]
        scenario="case_error",
        run_id="t",
        compressor=ContextCompressor(model_factory=lambda: fast_for_compress),
        archive=ContextArchive(tmp_path / strategy, run_id="t"),
    )


def test_both_strategies_produce_fail_kind_and_context(tmp_path, monkeypatch):
    for strategy in ("legacy", "managed"):
        result = _run(strategy, tmp_path, monkeypatch)
        assert result.strategy == strategy
        assert result.fail_kind == "case"
        assert result.context_text.strip()
        assert result.context_chars == len(result.context_text)
        assert result.tool_calls == 3
        assert result.latency_ms >= 0


def test_token_usage_includes_react_and_extraction(tmp_path, monkeypatch):
    result = _run("legacy", tmp_path, monkeypatch)
    # 4 次 ReAct 决策 + 1 次结构化抽取
    assert result.token_usage["calls"] == 5
    assert result.token_usage["input"] == 100 + 120 + 140 + 160 + 300
    assert result.token_usage["output"] == 10 + 10 + 10 + 20 + 40
    assert result.token_usage["total"] == (
        result.token_usage["input"] + result.token_usage["output"]
    )
    assert result.react_llm_calls == 4
    assert result.extract_llm_calls == 1
    assert result.react_prompt_chars_sum > 0
    assert result.react_token_input == 100 + 120 + 140 + 160
    assert result.extract_token_input == 300


def test_managed_reports_selected_ids_but_legacy_cannot(tmp_path, monkeypatch):
    managed = _run("managed", tmp_path, monkeypatch)
    legacy = _run("legacy", tmp_path, monkeypatch)

    # managed 能说清「最终上下文由哪些块组成」；legacy 按整块丢弃，没有这个粒度
    assert managed.selected_context_ids
    assert "goal" in managed.selected_context_ids
    assert legacy.selected_context_ids == []


def test_managed_dedups_repeated_tool_observation(tmp_path, monkeypatch):
    managed = _run("managed", tmp_path, monkeypatch)
    legacy = _run("legacy", tmp_path, monkeypatch)

    # 同一份 fetch_logs 被取了两次：managed 去重后只留一份
    fetch_ids = [i for i in managed.selected_context_ids if "fetch_logs" in i]
    assert len(fetch_ids) == 1
    # legacy 把重复摘录原样拼进上下文
    assert legacy.context_text.count("fetch_logs:") == 2


def test_managed_keeps_key_evidence_in_final_context(tmp_path, monkeypatch):
    managed = _run("managed", tmp_path, monkeypatch)
    assert "KeyError" in managed.context_text
    # 用户诉求是 immutable，必须活到最后一步
    assert "为什么报 KeyError" in managed.context_text


def test_managed_trims_react_history_legacy_does_not(tmp_path, monkeypatch):
    managed = _run("managed", tmp_path, monkeypatch)
    legacy = _run("legacy", tmp_path, monkeypatch)
    # legacy 不裁历史，永远是 0；managed 受 react_history_max_chars 约束
    assert legacy.trimmed_steps == 0
    assert managed.trimmed_steps >= 0


def test_managed_uses_shared_archive_and_reports_archived_n(tmp_path, monkeypatch):
    """managed：ReAct 裁剪与抽取压缩共用同一归档目录，archived_n 如实上报。"""
    _install(monkeypatch)
    fast_for_compress = _FakeFast()
    archive = ContextArchive(tmp_path / "shared", run_id="t")
    result = run_diagnosis(
        pipelines=_BRIEF,
        user_input="CaseA_235T_nmimo 为什么报 KeyError？",
        context_strategy="managed",
        scenario="case_error",
        run_id="t",
        compressor=ContextCompressor(model_factory=lambda: fast_for_compress),
        archive=archive,
    )
    assert result.archived_n == len(archive.refs)
    assert result.archived_n >= 0
    # 注入的 archive 目录就是唯一落盘点：两阶段共享，不另建 adhoc 目录
    assert not (tmp_path / "t").exists() or (tmp_path / "shared") in list(
        tmp_path.iterdir()
    )


def test_legacy_never_touches_archive(tmp_path, monkeypatch):
    """legacy：archive 恒为 None——不归档、archived_n=0、无回读工具。"""
    _install(monkeypatch)
    fast_for_compress = _FakeFast()
    legacy_archive = ContextArchive(tmp_path / "legacy", run_id="t")
    result = run_diagnosis(
        pipelines=_BRIEF,
        user_input="CaseA_235T_nmimo 为什么报 KeyError？",
        context_strategy="legacy",
        scenario="case_error",
        run_id="t",
        compressor=ContextCompressor(model_factory=lambda: fast_for_compress),
        archive=legacy_archive,
    )
    assert result.archived_n == 0
    # legacy 不该往注入的 archive 里写任何东西
    assert legacy_archive.refs == []
    assert not legacy_archive.directory.exists()


def test_node_output_shape_is_unchanged(tmp_path, monkeypatch):
    _install(monkeypatch)
    out = ea.error_analysis(
        {
            "pipelines": _BRIEF,
            "user_input": "为什么失败",
            "user_id": "u1",
            "task_id": "task-1",
        }
    )
    summary = out["summary"]
    assert summary["status"] == "ok"
    assert set(summary) == {"status", "message", "error_analysis", "analysis_text"}
    assert summary["error_analysis"]["fail_kind"] == "case"

    audit = out["audit"][0]
    assert audit["step"] == "error_analysis"
    assert audit["pipeline_ids"] == [_PID]
    assert audit["context_strategy"] == "managed"
    assert audit["context_chars"] > 0
    assert audit["token_usage"]["total"] > 0
    assert audit["tool_trace"]


def test_very_long_user_input_does_not_break_immutable_budget(tmp_path, monkeypatch):
    _install(monkeypatch)
    fast_for_compress = _FakeFast()
    # immutable 超预算的守卫是留给配置错误的，不该被一段超长用户输入触发
    result = run_diagnosis(
        pipelines=_BRIEF,
        user_input="为什么失败" * 5000,
        context_strategy="managed",
        scenario="case_error",
        run_id="t",
        compressor=ContextCompressor(model_factory=lambda: fast_for_compress),
        archive=ContextArchive(tmp_path / "long", run_id="t"),
    )
    assert result.fail_kind == "case"
    assert "goal" in result.selected_context_ids


def test_managed_without_transcript_writes_no_history(tmp_path, monkeypatch):
    result = _run("managed", tmp_path, monkeypatch)
    assert result.transcript_event_n == 0


def test_injected_transcript_records_loop_and_extract(tmp_path, monkeypatch):
    _install(monkeypatch)
    transcript = Transcript(
        MemoryTranscriptStore(),
        TranscriptScope("user-1", "task-1", "diagnose", "diag-exec"),
    )
    result = run_diagnosis(
        pipelines=_BRIEF,
        user_input="CaseA_235T_nmimo 为什么报 KeyError？",
        user_id="user-1",
        context_strategy="managed",
        scenario="case_error",
        run_id="task-1",
        compressor=ContextCompressor(model_factory=lambda: _FakeFast()),
        transcript=transcript,
    )
    assert result.fail_kind == "case"
    assert result.transcript_event_n > 0
    assert transcript.get("loop/start") is not None
    assert transcript.get("loop/end") is not None
    assert transcript.get("extract/result") is not None


def test_legacy_rejects_injected_transcript(tmp_path, monkeypatch):
    _install(monkeypatch)
    transcript = Transcript(
        MemoryTranscriptStore(),
        TranscriptScope("user-1", "task-1", "diagnose", "legacy-exec"),
    )
    with pytest.raises(ValueError, match="全量历史仅支持 managed"):
        run_diagnosis(
            pipelines=_BRIEF,
            user_input="为什么失败",
            context_strategy="legacy",
            transcript=transcript,
        )


def test_node_handles_empty_pipelines():
    out = ea.error_analysis({"pipelines": []})
    assert out["summary"]["status"] == "not_found"
    assert out["audit"][0]["status"] == "empty"


def test_default_diagnosis_engine_is_legacy(monkeypatch):
    """现网入口不切新引擎：error_analysis 默认仍走单次 ReAct。"""
    from work_agent.core.config import get_settings

    assert get_settings().profile.diagnosis_engine == "legacy"
    _install(monkeypatch)
    out = ea.error_analysis(
        {
            "pipelines": _BRIEF,
            "user_input": "为什么失败",
            "user_id": "u1",
            "task_id": "task-legacy-engine",
        }
    )
    assert out["summary"]["error_analysis"]["fail_kind"] == "case"
    assert out["audit"][0]["context_strategy"] == "managed"
    assert out["summary"]["error_analysis"].get("evidence_refs") == []
    assert out["summary"]["error_analysis"].get("stop_reason") == ""


def test_run_diagnosis_dispatches_component_parallel_engine():
    from work_agent.core.transcript import MemoryTranscriptStore, Transcript, TranscriptScope
    from work_agent.graph.helpers.diagnosis_models import InvestigateSpec, MainDecision

    transcript = Transcript(
        MemoryTranscriptStore(),
        TranscriptScope("user-1", "task-cp", "diagnose-main", "mainexec0000000000000000000000cp"),
    )
    decisions = [
        MainDecision(
            action="investigate",
            investigations=[InvestigateSpec(component="bbh", question="时钟")],
        ),
        MainDecision(
            action="conclude",
            fail_kind="env",
            root_component="bbh",
            evidence="ptp",
            conclusion="时钟问题",
            suggestion="修时钟",
            stop_reason="evidence_sufficient",
        ),
    ]

    def decide(snapshot):  # noqa: ANN001
        return decisions.pop(0)

    def investigate(task):  # noqa: ANN001
        from work_agent.core.usage import TokenUsage
        from work_agent.graph.helpers.diagnosis_models import ComponentReport, Evidence

        evidence = Evidence(
            evidence_id="ev-bbh-1",
            source="log",
            component="bbh",
            artifact_id="sha256_" + "e" * 64,
            excerpt="ptp_state=UNLOCKED",
        )
        report = ComponentReport(
            investigation_id=task.investigation_id,
            component="bbh",
            status="ok",
            findings=["时钟失锁"],
            evidence_ids=["ev-bbh-1"],
            coverage="bbh.log",
            tool_calls=1,
        )
        return report, [evidence], TokenUsage(), [{"type": "call", "name": "fetch_logs"}]

    result = run_diagnosis(
        pipelines=_BRIEF,
        user_input="BBL 激活超时",
        context_strategy="managed",
        scenario="bench06_bbh_clock_unlocked",
        run_id="task-cp",
        transcript=transcript,
        diagnosis_engine="component_parallel",
        decide_fn=decide,
        investigate_fn=investigate,
    )
    assert result.strategy == "component_parallel"
    assert result.fail_kind == "env"
    assert result.structured.get("evidence_refs") == ["ev-bbh-1"]
    assert result.structured.get("stop_reason") == "evidence_sufficient"
