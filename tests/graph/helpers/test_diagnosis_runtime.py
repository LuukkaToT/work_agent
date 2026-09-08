"""并行诊断运行时：并发屏障、汇总、重放、越界引用与分层日志场景。"""

from __future__ import annotations

import threading
import time
from dataclasses import replace

from work_agent.core.transcript import MemoryTranscriptStore, Transcript, TranscriptScope
from work_agent.core.usage import TokenUsage
from work_agent.graph.helpers.diagnosis_models import (
    ComponentReport,
    Evidence,
    Hypothesis,
    InvestigateSpec,
    InvestigationTask,
    LogScope,
    MainDecision,
)
from work_agent.graph.helpers.diagnosis_runtime import (
    DiagnosisBudget,
    DiagnosisSession,
    allocate_task_budget,
    collect_contradictions,
    dedupe_evidence,
    merge_reports,
    run_component_diagnosis,
    run_parallel_investigations,
    skip_reason,
)
from work_agent.graph.helpers.truncate import clip_text
from work_agent.tools.mock.logs import MockLogTool
from work_agent.tools.registry import get_pipeline_tool


def _report(component: str, status: str = "ok", **kwargs) -> ComponentReport:
    payload = {
        "investigation_id": kwargs.pop("investigation_id", f"inv-{component}"),
        "component": component,
        "status": status,
        "findings": kwargs.pop("findings", [f"{component} 发现"]),
        "coverage": kwargs.pop("coverage", f"{component}.log tail=200"),
    }
    payload.update(kwargs)
    return ComponentReport.model_validate(payload)


def _task(component: str, *, question: str = "q", tail: int = 200, inv: str = "") -> InvestigationTask:
    return InvestigationTask(
        investigation_id=inv or f"inv-{component}-{tail}",
        diagnosis_task_id="task-1",
        round_index=1,
        component=component,
        question=question,
        pipeline_id="p1",
        log_scope=LogScope(pipeline_id="p1", component=component, tail_lines=tail),
    )


def _evidence(eid: str, component: str, excerpt: str, artifact: str = "sha256_" + "b" * 64) -> Evidence:
    return Evidence(
        evidence_id=eid,
        source="log",
        component=component,
        artifact_id=artifact,
        excerpt=excerpt,
        file=f"{component}.log",
    )


def test_three_components_overlap_and_max_workers_caps():
    barrier = threading.Barrier(3)
    overlapped = threading.Event()

    def overlap_runner(task: InvestigationTask):
        barrier.wait(timeout=2)
        overlapped.set()
        return (
            _report(task.component, investigation_id=task.investigation_id),
            [],
            TokenUsage(),
            [],
        )

    tasks = [_task(name) for name in ("bbh", "bbl", "comm")]
    results = run_parallel_investigations(tasks, overlap_runner, max_workers=3)
    assert overlapped.is_set()
    assert {item[0].component for item in results} == {"bbh", "bbl", "comm"}

    active = 0
    peak = 0
    lock = threading.Lock()

    def capped_runner(task: InvestigationTask):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.15)
        with lock:
            active -= 1
        return (
            _report(task.component, investigation_id=task.investigation_id),
            [],
            TokenUsage(),
            [],
        )

    run_parallel_investigations(tasks, capped_runner, max_workers=2)
    assert peak <= 2
    assert peak >= 1


def test_aggregator_dedupes_evidence_merges_reports_keeps_contradictions():
    e1 = _evidence("ev-1", "bbh", "ptp_state=UNLOCKED")
    e2 = _evidence("ev-2", "bbh", "ptp_state=UNLOCKED")  # 同内容哈希
    e3 = _evidence("ev-3", "bbl", "E-BBL-3112")
    assert [item.evidence_id for item in dedupe_evidence([e1, e2, e3])] == ["ev-1", "ev-3"]

    reports = merge_reports(
        [
            _report("bbl", investigation_id="inv-b"),
            _report("bbh", investigation_id="inv-a"),
            _report("bbh", investigation_id="inv-a", findings=["后写覆盖"]),
        ]
    )
    assert [item.component for item in reports] == ["bbh", "bbl"]
    assert reports[0].findings == ["后写覆盖"]

    hyp = Hypothesis(
        hypothesis_id="h1",
        description="BBH 时钟失锁",
        candidate_components=["bbh"],
        supporting_evidence_ids=["ev-1"],
        contradicting_evidence_ids=["ev-9"],
    )
    lines = collect_contradictions(
        [
            _report("bbh", supports_hypotheses=["h1"]),
            _report("comm", contradicts_hypotheses=["h1"]),
        ],
        [hyp],
    )
    assert any("支持与反证" in line or "反证来自" in line for line in lines)


def test_duplicate_skip_allows_retry_and_expanded_range():
    session = DiagnosisSession()
    first = _task("bbh", question="时钟是否失锁", tail=100, inv="inv-1")
    session.tasks[first.investigation_id] = first
    session.reports[first.investigation_id] = _report("bbh", investigation_id="inv-1")
    dup = _task("bbh", question="时钟是否失锁", tail=100, inv="inv-2")
    assert skip_reason(dup, session) == "duplicate"

    session.reports[first.investigation_id] = _report(
        "bbh", status="timeout", investigation_id="inv-1"
    )
    assert skip_reason(dup, session) is None

    wider = _task("bbh", question="时钟是否失锁", tail=800, inv="inv-3")
    session.reports[first.investigation_id] = _report("bbh", investigation_id="inv-1")
    assert skip_reason(wider, session) is None
    assert wider.log_scope.covers_more_than(first.log_scope)


def test_allocate_caps_to_remaining_tools_and_generates_ids_before_dispatch():
    budget = DiagnosisBudget(max_tool_calls=5, max_workers=3)
    budget.charge(3)
    specs = [
        InvestigateSpec(component="bbh", question="时钟"),
        InvestigateSpec(component="bbl", question="激活"),
        InvestigateSpec(component="comm", question="链路"),
    ]
    tasks = allocate_task_budget(
        specs,
        budget=budget,
        pipeline_id="p1",
        diagnosis_task_id="t1",
        round_index=1,
        version_tag="27B",
        investigation_ids=["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
    )
    assert len(tasks) == 2
    assert tasks[0].investigation_id == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert tasks[0].budget.max_steps >= 1


def _log_runner(scenario: str, pipeline_id: str):
    tool = MockLogTool(scenario)

    def _run(task: InvestigationTask):
        text = tool.fetch_logs(pipeline_id, tail_lines=task.log_scope.tail_lines or 200, component=task.component)
        status = "ok" if "ERROR" in text or "subscription_ack_missing" in text or "request_subscribe" in text else "no_hit"
        if status == "no_hit" and "verdict=pass" in text:
            findings = ["指定范围内未命中故障模式，不能据此判定组件正常"]
            status = "no_hit"
        elif status == "no_hit":
            findings = ["指定范围内未命中相关日志，不能据此判定组件正常"]
        else:
            findings = [clip_text(text, max_chars=240)]
        excerpt = clip_text(text, max_chars=400)
        artifact = "sha256_" + (task.component.encode().hex() + "0" * 64)[:64]
        evidence = []
        if status == "ok":
            evidence = [
                Evidence(
                    evidence_id=f"ev-{task.component}-1",
                    source="log",
                    component=task.component,
                    artifact_id=artifact,
                    excerpt=excerpt,
                    file=f"{task.component}.log",
                    collection_condition=f"pipeline_id={pipeline_id} component={task.component}",
                )
            ]
        report = ComponentReport(
            investigation_id=task.investigation_id,
            component=task.component,
            status=status,  # type: ignore[arg-type]
            findings=findings,
            evidence_ids=[item.evidence_id for item in evidence],
            coverage=f"{task.component}.log tail={task.log_scope.tail_lines}",
            missing=[] if status == "ok" else ["本组件故障原文"],
            tool_calls=1,
        )
        return report, evidence, TokenUsage(), [{"type": "call", "name": "fetch_logs"}]

    return _run


def _start_pipeline(scenario: str) -> str:
    meta = {
        "bench06_bbh_clock_unlocked": ("NR_BENCH_006_PTP_UNLOCK", "7.223.50.65"),
        "bench14_cell_load_cascade_from_rat": ("NR_BENCH_014_RAT_SCS_CASCADE", "7.223.50.73"),
        "bench08_rx_subscription_debug_stall": ("NR_BENCH_008_RX_SUB_STALL", "7.223.50.67"),
        "bench20_transient_subscription_recovered": ("NR_BENCH_020_TRANSIENT_RECOVERY", "7.223.50.79"),
        "bench15_cell_activation_timeout_clock": ("NR_BENCH_015_CLOCK_HOLDOVER", "7.223.50.74"),
    }
    case_name, env = meta[scenario]
    pipe = get_pipeline_tool(scenario=scenario)
    handle = pipe.create(case_names=[case_name], version="27B", physical_env=env)
    pipe.start(handle.pipeline_id)
    return handle.pipeline_id


def _memory_transcript(task_id: str = "task-diag-1") -> Transcript:
    return Transcript(
        MemoryTranscriptStore(),
        TranscriptScope("user-1", task_id, "diagnose-main", "mainexec000000000000000000000001"),
    )


def _run_engine(scenario: str, decide, runner, *, transcript=None, **kwargs):
    pid = _start_pipeline(scenario)
    live = transcript or _memory_transcript()
    return run_component_diagnosis(
        pipelines=[
            {
                "pipeline_id": pid,
                "case_names": ["c"],
                "version": "27B",
                "env": "x",
                "status": "failed",
            }
        ],
        user_input="请诊断根因",
        scenario=scenario,
        run_id="task-diag-1",
        transcript=live,
        decide_fn=decide,
        investigate_fn=runner or _log_runner(scenario, pid),
        **kwargs,
    ), pid


def test_bbh_clock_and_bbl_timeout_correlate_comm_only_narrows():
    scenario = "bench15_cell_activation_timeout_clock"
    pid_holder: dict[str, str] = {}

    def decide(snapshot: dict) -> MainDecision:
        pid_holder["n"] = snapshot["round"]
        if snapshot["round"] == 1:
            return MainDecision(
                action="investigate",
                investigations=[
                    InvestigateSpec(component="bbh", question="时钟是否 holdover"),
                    InvestigateSpec(component="bbl", question="激活超时原因"),
                    InvestigateSpec(component="comm", question="链路是否也失败"),
                ],
            )
        ids = snapshot["known_evidence_ids"]
        return MainDecision(
            action="conclude",
            fail_kind="env",
            root_component="bbh",
            root_cause="时钟 holdover 导致 BBL 激活超时",
            evidence="holdover_ms=65000",
            evidence_ids=ids,
            conclusion="BBH 时钟失锁，BBL 激活超时是相关现象；COMM 无故障命中只缩小范围",
            suggestion="先恢复时钟源",
            stop_reason="evidence_sufficient",
        )

    result, _pid = _run_engine(scenario, decide, None)
    assert result.fail_kind == "env"
    assert result.root_component == "bbh"
    findings = "\n".join(result.structured.get("component_findings") or [])
    assert "bbh" in findings and "bbl" in findings
    assert "comm[no_hit]" in findings or "comm[" in findings
    assert "holdover_ms=65000" in result.context_text or "holdover" in result.structured.get("evidence", "")
    assert result.structured.get("stop_reason")


def test_cascade_handshake_stall_transient_and_contradiction():
    # 级联：RAT 根因，BBH/BBL 是 cascade
    def decide_cascade(snapshot: dict) -> MainDecision:
        if snapshot["round"] == 1:
            return MainDecision(
                action="investigate",
                investigations=[
                    InvestigateSpec(component="rat", question="是否配置拒绝"),
                    InvestigateSpec(component="bbh", question="是否只是级联"),
                    InvestigateSpec(component="bbl", question="是否只是级联"),
                ],
            )
        return MainDecision(
            action="conclude",
            fail_kind="case",
            root_component="rat",
            root_cause="SCS 不合法",
            evidence="field=scs_khz",
            evidence_ids=snapshot["known_evidence_ids"],
            conclusion="RAT 配置拒绝早于 BBH/BBL 级联错误",
            suggestion="改用例 SCS",
            stop_reason="evidence_sufficient",
        )

    cascade, _ = _run_engine("bench14_cell_load_cascade_from_rat", decide_cascade, None)
    assert cascade.root_component == "rat"
    assert "scs_khz" in (cascade.structured.get("evidence") or "") or "rat" in cascade.context_text

    def decide_stall(snapshot: dict) -> MainDecision:
        if snapshot["round"] == 1:
            return MainDecision(
                action="investigate",
                investigations=[
                    InvestigateSpec(component="bbh", question="RX 订阅是否缺 ACK"),
                    InvestigateSpec(component="bbl", question="订阅表是否为空"),
                ],
            )
        return MainDecision(
            action="conclude",
            fail_kind="env",
            root_component="bbh",
            evidence="subscription_ack_missing",
            evidence_ids=snapshot["known_evidence_ids"],
            conclusion="无 ERROR 但 RX 订阅未闭环",
            suggestion="查 BBH 到 BBL 通路",
            stop_reason="evidence_sufficient",
        )

    stall, _ = _run_engine("bench08_rx_subscription_debug_stall", decide_stall, None)
    assert "subscription_ack_missing" in stall.context_text or "ack" in stall.structured.get("evidence", "")

    def decide_ok(snapshot: dict) -> MainDecision:
        if snapshot["round"] == 1:
            return MainDecision(
                action="investigate",
                investigations=[InvestigateSpec(component="bbh", question="订阅是否恢复")],
            )
        return MainDecision(
            action="conclude",
            fail_kind="none",
            root_component="none",
            evidence_ids=snapshot["known_evidence_ids"],
            conclusion="短暂重试后已 ESTABLISHED",
            suggestion="无需处理",
            stop_reason="healthy",
        )

    recovered, _ = _run_engine("bench20_transient_subscription_recovered", decide_ok, None)
    assert recovered.fail_kind in {"none", "unknown"}

    session = DiagnosisSession()
    session.reports["a"] = _report("bbh", supports_hypotheses=["h1"], investigation_id="a")
    session.reports["b"] = _report("comm", contradicts_hypotheses=["h1"], investigation_id="b")
    session.hypotheses["h1"] = Hypothesis(
        hypothesis_id="h1",
        description="x",
        candidate_components=["bbh"],
        supporting_evidence_ids=["e1"],
        contradicting_evidence_ids=["e2"],
    )
    assert collect_contradictions(session.report_list(), list(session.hypotheses.values()))


def test_fake_evidence_refs_are_rejected():
    def decide(snapshot: dict) -> MainDecision:
        if snapshot["round"] == 1:
            return MainDecision(
                action="investigate",
                investigations=[InvestigateSpec(component="bbh", question="时钟")],
            )
        return MainDecision(
            action="conclude",
            fail_kind="env",
            root_component="bbh",
            evidence_ids=["ev-forged-not-exist"],
            conclusion="伪造引用",
            stop_reason="should_be_rejected",
        )

    result, _ = _run_engine("bench06_bbh_clock_unlocked", decide, None)
    assert result.fail_kind == "unknown"
    assert result.structured.get("stop_reason") == "invalid_evidence_refs"
    assert "拒绝未登记证据引用" in " ".join(result.structured.get("missing_info") or [])


def test_replay_does_not_rerun_completed_investigation(monkeypatch):
    calls: list[str] = []

    def runner(task: InvestigationTask):
        calls.append(task.investigation_id)
        return (
            _report(task.component, investigation_id=task.investigation_id, tool_calls=1),
            [
                _evidence(
                    f"ev-{task.component}-1",
                    task.component,
                    "ptp_state=UNLOCKED",
                    artifact="sha256_" + "c" * 64,
                )
            ],
            TokenUsage(),
            [{"type": "call", "name": "fetch_logs"}],
        )

    import work_agent.graph.helpers.diagnosis_runtime as runtime

    original_alloc = runtime.allocate_task_budget

    def alloc_with_fixed(specs, **kwargs):
        kwargs["investigation_ids"] = ["replayidreplayidreplayidreplayid"]
        return original_alloc(specs, **kwargs)

    monkeypatch.setattr(runtime, "allocate_task_budget", alloc_with_fixed)

    def decide(snapshot: dict) -> MainDecision:
        return MainDecision(
            action="investigate",
            investigations=[InvestigateSpec(component="bbh", question="时钟是否失锁")],
        )

    scenario = "bench06_bbh_clock_unlocked"
    pid = _start_pipeline(scenario)
    run_component_diagnosis(
        pipelines=[{"pipeline_id": pid, "case_names": ["c"], "version": "27B", "status": "failed"}],
        user_input="诊断",
        scenario=scenario,
        run_id="task-replay",
        transcript=_memory_transcript("task-replay"),
        decide_fn=decide,
        investigate_fn=runner,
    )
    assert calls == ["replayidreplayidreplayidreplayid"]


def test_budget_exhaustion_and_timeout_uncertainty(monkeypatch):
    def decide(snapshot: dict) -> MainDecision:
        return MainDecision(
            action="investigate",
            investigations=[InvestigateSpec(component="bbh", question="时钟")],
        )

    def slow(task: InvestigationTask):
        time.sleep(0.4)
        return (
            _report("bbh", status="timeout", investigation_id=task.investigation_id, missing=["超时"]),
            [],
            TokenUsage(),
            [],
        )

    import work_agent.graph.helpers.diagnosis_runtime as runtime
    from work_agent.core.config import get_settings

    settings = get_settings()
    tight = replace(
        settings,
        profile=replace(
            settings.profile,
            diagnosis_max_rounds=1,
            diagnosis_max_tool_calls=2,
            diagnosis_timeout_seconds=1,
            diagnosis_component_timeout_seconds=1,
        ),
    )
    monkeypatch.setattr(runtime, "get_settings", lambda: tight)

    scenario = "bench06_bbh_clock_unlocked"
    pid = _start_pipeline(scenario)
    result = run_component_diagnosis(
        pipelines=[{"pipeline_id": pid, "case_names": ["c"], "version": "27B", "status": "failed"}],
        user_input="诊断",
        scenario=scenario,
        run_id="task-budget",
        transcript=_memory_transcript("task-budget"),
        decide_fn=decide,
        investigate_fn=slow,
    )
    assert result.fail_kind == "unknown"
    assert result.structured.get("stop_reason") in {
        "budget_tools",
        "budget_timeout",
        "max_rounds",
        "insufficient_evidence",
    }


def test_transcript_keeps_original_and_context_is_bounded():
    transcript = Transcript(
        MemoryTranscriptStore(),
        TranscriptScope("user-1", "task-1", "diagnose-main", "mainexec000000000000000000000000"),
    )
    long_excerpt = "E-BBH-2101 ptp_state=UNLOCKED " + ("x" * 5000)

    def runner(task: InvestigationTask):
        evidence = _evidence("ev-bbh-1", "bbh", long_excerpt, artifact="sha256_" + "d" * 64)
        return (
            _report("bbh", investigation_id=task.investigation_id, tool_calls=1),
            [evidence],
            TokenUsage(),
            [{"type": "call", "name": "fetch_logs"}],
        )

    def decide(snapshot: dict) -> MainDecision:
        if snapshot["round"] == 1:
            return MainDecision(
                action="investigate",
                investigations=[InvestigateSpec(component="bbh", question="时钟")],
            )
        return MainDecision(
            action="conclude",
            fail_kind="env",
            root_component="bbh",
            evidence_ids=["ev-bbh-1"],
            evidence="E-BBH-2101",
            conclusion="时钟失锁",
            suggestion="修时钟",
            stop_reason="evidence_sufficient",
        )

    result, _ = _run_engine(
        "bench06_bbh_clock_unlocked",
        decide,
        runner,
        transcript=transcript,
    )
    assert result.transcript_event_n > 0
    assert transcript.get("diagnosis/start") is not None
    assert "E-BBH-2101" in result.context_text
    assert result.context_chars == len(result.context_text)
    assert result.context_chars <= 40000
