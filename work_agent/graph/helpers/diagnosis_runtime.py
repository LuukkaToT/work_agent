"""
并行组件诊断运行时：主 ReAct 派发调查，组件 Agent 并行取证，代码汇总后再归因。

不把组件 Agent 暴露为 MCP tool。并行用线程池调用现有 ``run_agent_loop``，
本轮不做 LangGraph 组件子图。汇总纯代码，不让模型改写原文或抹平矛盾。
"""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from work_agent.core.components import get_component
from work_agent.core.config import get_settings
from work_agent.core.investigation_journal import (
    InvestigationInProgress,
    InvestigationJournal,
    InvestigationTaskRecord,
    MemoryInvestigationJournal,
    PostgresInvestigationJournal,
)
from work_agent.core.llm import get_reasoning_model
from work_agent.core.transcript import (
    MemoryTranscriptStore,
    PostgresTranscriptStore,
    Transcript,
    TranscriptScope,
)
from work_agent.core.usage import TokenUsage, usage_from_message
from work_agent.graph.helpers.component_agent import run_component_investigation
from work_agent.graph.helpers.context_budget import (
    PRIORITY_CONCLUSION,
    PRIORITY_EVIDENCE,
    PRIORITY_RULED_OUT,
)
from work_agent.graph.helpers.context_manager import ContextManager
from work_agent.graph.helpers.context_selector import (
    PIN_IMMUTABLE,
    PIN_NORMAL,
    PIN_PROTECTED,
    ContextItem,
)
from work_agent.graph.helpers.diagnose_tools import build_diagnose_tools
from work_agent.graph.helpers.log_bootstrap import scan_failure_cues
from work_agent.graph.helpers.diagnosis_models import (
    ComponentReport,
    Evidence,
    Hypothesis,
    InvestigateSpec,
    InvestigationBudget,
    InvestigationTask,
    LogScope,
    MainDecision,
)
from work_agent.graph.helpers.transcript_recorder import TranscriptRecorder
from work_agent.graph.helpers.truncate import CharBudget, clip_text
from work_agent.tools.mock.scenarios import MockScenario

_GOAL_MAX_CHARS = 2000
_ORCHESTRATOR_SYSTEM = """你是诊断编排器，不是再搜一遍全量日志的取证员。

根据流水线概览、failure_cues、各组件报告摘要、证据引用和缺口，输出结构化动作：
- investigate：本轮最多 3 个组件调查，写清待验证问题；不强制全查六组件。
- conclude：证据充分时结案。必须解释相关现象和关键反证。
  时间先后或组件依赖本身不能单独当因果。
- insufficient_evidence：预算将尽或资料不足时给出候选原因与缺失。

硬约束：
- 首轮必须根据 failure_cues 提出少量可验证假设，只派相关组件；不得因为没把握就全查六组件。
- cues 与错误码目录里的 probable_components 是路由提示，不是根因；cascade=true / DEPENDENCY_FAILED 优先查上游。
- 结论引用的 evidence_ids 必须是已给出的证据 ID，禁止编造。
- 知识检索只是旁证。
- 无命中只缩小范围，不能写成该组件正常。
- 级联 ERROR / DEPENDENCY_FAILED / cascade=true 通常不是首个根因。
- 使用中文。
"""

InvestigateFn = Callable[
    [InvestigationTask],
    tuple[ComponentReport, list[Evidence], TokenUsage, list[dict[str, Any]]],
]
DecideFn = Callable[[dict[str, Any]], MainDecision]


@dataclass
class DiagnosisBudget:
    """全诊断预算。未用额度在组件返回后自动留在 remaining 里。"""

    max_rounds: int = 3
    max_tool_calls: int = 24
    max_seconds: float = 300.0
    max_workers: int = 3
    component_max_steps: int = 4
    component_timeout_seconds: float = 90.0
    used_tool_calls: int = 0
    started_at: float = field(default_factory=time.perf_counter)

    def remaining_tools(self) -> int:
        return max(0, self.max_tool_calls - self.used_tool_calls)

    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - (time.perf_counter() - self.started_at))

    def charge(self, tool_calls: int) -> None:
        self.used_tool_calls += max(0, tool_calls)


@dataclass
class DiagnosisSession:
    """一次并行诊断的可变状态；不写入图 checkpoint。

    调查进度由 InvestigationJournal 持久化：SUCCEEDED 回放 result_ref，
    EXPIRED 换新 execution_id 重跑。本对象只是本进程内的工作副本。
    """

    reports: dict[str, ComponentReport] = field(default_factory=dict)
    tasks: dict[str, InvestigationTask] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    hypotheses: dict[str, Hypothesis] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)

    def evidence_list(self) -> list[Evidence]:
        return dedupe_evidence(self.evidence.values())

    def report_list(self) -> list[ComponentReport]:
        return merge_reports(self.reports.values())


def dedupe_evidence(items: Sequence[Evidence]) -> list[Evidence]:
    """按证据内容哈希去重，保留先到者。"""
    seen: set[str] = set()
    out: list[Evidence] = []
    for item in items:
        key = item.content_hash()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def merge_reports(reports: Sequence[ComponentReport]) -> list[ComponentReport]:
    """按调查 ID 幂等合并，再按稳定组件名排序。后写覆盖同 ID。"""
    by_id: dict[str, ComponentReport] = {}
    for report in reports:
        by_id[report.investigation_id] = report
    return sorted(by_id.values(), key=lambda item: (item.component, item.investigation_id))


def collect_contradictions(
    reports: Sequence[ComponentReport],
    hypotheses: Sequence[Hypothesis],
) -> list[str]:
    """保留矛盾：同一假设既有支持又有反证，或不同报告对同一假设立场相反。"""
    lines: list[str] = []
    by_hyp: dict[str, Hypothesis] = {item.hypothesis_id: item for item in hypotheses}
    for hyp in by_hyp.values():
        if hyp.supporting_evidence_ids and hyp.contradicting_evidence_ids:
            lines.append(
                f"假设 {hyp.hypothesis_id} 同时存在支持与反证，不能抹平"
            )
    support: dict[str, set[str]] = {}
    contra: dict[str, set[str]] = {}
    for report in reports:
        for hid in report.supports_hypotheses:
            support.setdefault(hid, set()).add(report.component)
        for hid in report.contradicts_hypotheses:
            contra.setdefault(hid, set()).add(report.component)
    for hid in sorted(set(support) | set(contra)):
        both = support.get(hid, set()) & contra.get(hid, set())
        if support.get(hid) and contra.get(hid):
            lines.append(
                f"假设 {hid}：支持来自 {sorted(support[hid])}，反证来自 {sorted(contra[hid])}"
                + (f"（{sorted(both)} 内部也矛盾）" if both else "")
            )
    return lines


def skip_reason(
    task: InvestigationTask,
    session: DiagnosisSession,
) -> str | None:
    """
    同组件 + 同问题 + 同日志范围默认拒绝。

    允许：范围扩大、失败重试。新证据由扩大范围或新问题表达。
    已完成的 investigation_id 走重放，不在这里拒绝。
    """
    if task.investigation_id in session.reports:
        return None
    for prev in session.tasks.values():
        if prev.investigation_id == task.investigation_id:
            continue
        if prev.fingerprint() != task.fingerprint():
            continue
        prev_report = session.reports.get(prev.investigation_id)
        if prev_report is not None and prev_report.status in {"timeout", "tool_error"}:
            return None
        return "duplicate"
    return None


def allocate_task_budget(
    specs: Sequence[InvestigateSpec],
    *,
    budget: DiagnosisBudget,
    pipeline_id: str,
    diagnosis_task_id: str,
    round_index: int,
    version_tag: str,
    investigation_ids: Sequence[str] | None = None,
) -> list[InvestigationTask]:
    """派发前切分剩余工具次数与超时；每任务独立调查 ID。"""
    n = min(len(specs), budget.remaining_tools(), budget.max_workers)
    specs = list(specs)[:n]
    if n <= 0:
        return []
    remain = budget.remaining_tools()
    if remain <= 0 or budget.remaining_seconds() <= 0:
        return []
    per_steps = max(1, min(budget.component_max_steps, remain // n or 1))
    timeout = min(budget.component_timeout_seconds, budget.remaining_seconds())
    tasks: list[InvestigationTask] = []
    for idx, spec in enumerate(specs):
        spec_ok = get_component(spec.component)
        inv_id = (
            investigation_ids[idx]
            if investigation_ids is not None and idx < len(investigation_ids)
            else uuid.uuid4().hex
        )
        scope = LogScope(
            pipeline_id=pipeline_id,
            component=spec_ok.id,
            tail_lines=spec.tail_lines,
            start_ts=spec.start_ts,
            end_ts=spec.end_ts,
            version_tag=version_tag,
        )
        tasks.append(
            InvestigationTask(
                investigation_id=inv_id,
                diagnosis_task_id=diagnosis_task_id,
                round_index=round_index,
                component=spec_ok.id,
                question=spec.question,
                related_hypothesis_ids=list(spec.related_hypothesis_ids),
                pipeline_id=pipeline_id,
                log_scope=scope,
                budget=InvestigationBudget(
                    max_steps=per_steps,
                    timeout_seconds=timeout,
                ),
            )
        )
    return tasks


def run_parallel_investigations(
    tasks: Sequence[InvestigationTask],
    runner: InvestigateFn,
    *,
    max_workers: int = 3,
) -> list[tuple[ComponentReport, list[Evidence], TokenUsage, list[dict[str, Any]]]]:
    """
    线程池并行跑组件调查。

    ``max_workers`` 是并发上限；任务数更少时按任务数收缩。
    """
    if not tasks:
        return []
    workers = max(1, min(max_workers, len(tasks)))
    ordered = list(tasks)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(runner, task) for task in ordered]
        return [fut.result() for fut in futures]


def run_component_diagnosis(
    *,
    pipelines: Sequence[Mapping[str, Any]],
    user_input: str = "",
    user_id: str = "",
    scenario: MockScenario = "case_error",
    run_id: str = "",
    transcript: Transcript | None = None,
    decide_fn: DecideFn | None = None,
    investigate_fn: InvestigateFn | None = None,
    reasoning_model: BaseChatModel | None = None,
    compressor: Any = None,
    journal: InvestigationJournal | None = None,
) -> Any:
    """
    并行组件诊断内核，供 ``run_diagnosis`` 与 eval 注入。

    新引擎强制 transcript。主 ReAct 只看报告摘要、证据引用和缺口。
    调查任务走 journal：同一 run_id 恢复时 SUCCEEDED 不重跑，EXPIRED 换 attempt。
    """
    from work_agent.graph.nodes.error_analysis import DiagnosisResult, diagnosis_conclusion

    started = time.perf_counter()
    profile = get_settings().profile
    budget = DiagnosisBudget(
        max_rounds=profile.diagnosis_max_rounds,
        max_tool_calls=profile.diagnosis_max_tool_calls,
        max_seconds=float(profile.diagnosis_timeout_seconds),
        max_workers=profile.diagnosis_max_workers,
        component_max_steps=profile.diagnosis_component_max_steps,
        component_timeout_seconds=float(profile.diagnosis_component_timeout_seconds),
    )
    usage = TokenUsage()
    session = DiagnosisSession()
    main_transcript = _bind_main_transcript(
        user_id=user_id, task_id=run_id, injected=transcript
    )
    store = main_transcript.store_backend
    live_journal = _bind_journal(journal, store)
    recorder = TranscriptRecorder(main_transcript)
    pipeline_id = _first_pipeline_id(pipelines)
    version_tag = _version_tag(pipelines)
    diagnosis_task_id = _scope_id(run_id, "adhoc")
    run_rec = live_journal.ensure_run(
        diagnosis_task_id,
        user_id=_scope_id(user_id, "anonymous"),
        pipeline_id=pipeline_id,
        max_rounds=budget.max_rounds,
        max_tool_calls=budget.max_tool_calls,
    )

    def _charge_tool() -> None:
        live_journal.add_used_tool_calls(diagnosis_task_id, 1)

    overview, _init_calls, init_trace = _init_overview(
        pipeline_id,
        scenario=scenario,
        transcript=main_transcript,
        on_tool_start=_charge_tool,
    )
    _sync_budget(budget, live_journal, diagnosis_task_id)
    session.tool_trace.extend(init_trace)
    recorder.messages(
        "diagnosis/start",
        "diagnosis_start",
        [SystemMessage(content=_ORCHESTRATOR_SYSTEM), HumanMessage(content=overview[:4000])],
        metadata={"pipeline_id": pipeline_id, "engine": "component_parallel"},
    )

    inner_runner = investigate_fn or _make_default_runner(
        store=store,
        main_scope=main_transcript.scope,
        scenario=scenario,
        user_id=user_id,
        on_tool_start=_charge_tool,
    )
    runner = _wrap_journal_runner(
        inner_runner,
        journal=live_journal,
        store=store,
        main_scope=main_transcript.scope,
    )
    decide = decide_fn or _make_llm_decide(reasoning_model or get_reasoning_model(temperature=0))

    recovered_usage = _restore_from_journal(
        journal=live_journal,
        run_id=diagnosis_task_id,
        store=store,
        main_scope=main_transcript.scope,
        session=session,
        budget=budget,
        runner=runner,
    )
    usage = usage + recovered_usage
    _sync_budget(budget, live_journal, diagnosis_task_id)

    stop_reason = "max_rounds"
    last_decision = MainDecision(action="insufficient_evidence", stop_reason="max_rounds")
    selected_ids: list[str] = []
    context_text = ""
    context_chars = 0
    react_context_chars = 0
    start_round = max(1, run_rec.current_round)

    for round_index in range(start_round, budget.max_rounds + 1):
        if budget.remaining_seconds() <= 0:
            stop_reason = "budget_timeout"
            last_decision = MainDecision(
                action="insufficient_evidence",
                stop_reason=stop_reason,
                missing_info=["诊断累计超时"],
            )
            break
        context_text, selected_ids, context_chars, protected = _compose_main_context(
            user_input=user_input,
            overview=overview,
            session=session,
            budget=budget,
            round_index=round_index,
            transcript=main_transcript,
            compressor=compressor,
        )
        react_context_chars = context_chars
        snapshot = _decision_snapshot(
            user_input=user_input,
            overview=overview,
            session=session,
            budget=budget,
            round_index=round_index,
            context_text=context_text,
        )
        recorder.messages(
            f"diagnosis/round/{round_index:02d}/input",
            "orchestrator_input",
            [SystemMessage(content=_ORCHESTRATOR_SYSTEM), HumanMessage(content=context_text)],
            metadata={"round": round_index, "selected_ids": selected_ids, "protected": protected},
        )
        decision, decide_usage = _call_decide(decide, snapshot)
        usage = usage + decide_usage
        last_decision = _sanitize_decision(decision, session)
        main_transcript.record(
            f"diagnosis/round/{round_index:02d}/decision",
            "orchestrator_decision",
            {
                "action": last_decision.action,
                "stop_reason": last_decision.stop_reason,
                "investigation_n": len(last_decision.investigations),
            },
        )
        _merge_hypotheses(session, last_decision.hypotheses)

        live_journal.update_run(diagnosis_task_id, current_round=round_index)
        if last_decision.action != "investigate":
            stop_reason = last_decision.stop_reason or last_decision.action
            break
        if budget.remaining_tools() <= 0:
            stop_reason = "budget_tools"
            last_decision = last_decision.model_copy(
                update={"action": "insufficient_evidence", "stop_reason": stop_reason}
            )
            break

        reuse_ids = _succeeded_ids_by_fingerprint(live_journal, diagnosis_task_id)
        tasks = allocate_task_budget(
            last_decision.investigations[: budget.max_workers],
            budget=budget,
            pipeline_id=pipeline_id,
            diagnosis_task_id=diagnosis_task_id,
            round_index=round_index,
            version_tag=version_tag,
        )
        tasks = [
            task.model_copy(update={"investigation_id": reuse_ids[task.fingerprint()]})
            if task.fingerprint() in reuse_ids
            else task
            for task in tasks
        ]
        runnable: list[InvestigationTask] = []
        for task in tasks:
            cached = session.reports.get(task.investigation_id)
            if cached is not None:
                session.skipped.append(f"replay:{task.investigation_id}")
                continue
            reason = skip_reason(task, session)
            if reason == "duplicate":
                session.skipped.append(f"duplicate:{task.component}:{task.question}")
                continue
            live_journal.persist_pending(
                investigation_id=task.investigation_id,
                run_id=diagnosis_task_id,
                round_index=task.round_index,
                component=task.component,
                question=task.question,
                log_scope=task.log_scope.model_dump(mode="json"),
            )
            session.tasks[task.investigation_id] = task
            runnable.append(task)
        if not runnable:
            stop_reason = "duplicate_or_replay"
            if round_index == budget.max_rounds:
                last_decision = last_decision.model_copy(
                    update={
                        "action": "insufficient_evidence",
                        "stop_reason": stop_reason,
                        "missing_info": list(last_decision.missing_info)
                        + ["本轮调查均为重复或重放，已停止"],
                    }
                )
            continue

        results = run_parallel_investigations(
            runnable, runner, max_workers=budget.max_workers
        )
        for (report, evidences, inv_usage, trace), task in zip(results, runnable):
            usage = usage + inv_usage
            session.reports[report.investigation_id] = report
            session.tasks[task.investigation_id] = task
            session.tool_trace.extend(trace)
            for item in evidences:
                session.evidence[item.evidence_id] = item
        _sync_budget(budget, live_journal, diagnosis_task_id)
        if round_index == budget.max_rounds and last_decision.action == "investigate":
            stop_reason = "max_rounds"

    if last_decision.action == "investigate":
        context_text, selected_ids, context_chars, protected = _compose_main_context(
            user_input=user_input,
            overview=overview,
            session=session,
            budget=budget,
            round_index=budget.max_rounds,
            transcript=main_transcript,
            compressor=compressor,
        )
        snapshot = _decision_snapshot(
            user_input=user_input,
            overview=overview,
            session=session,
            budget=budget,
            round_index=budget.max_rounds,
            context_text=context_text,
        )
        final_decision, decide_usage = _call_decide(decide, snapshot)
        usage = usage + decide_usage
        last_decision = _sanitize_decision(final_decision, session)
        _merge_hypotheses(session, last_decision.hypotheses)
        if last_decision.action == "investigate":
            last_decision = last_decision.model_copy(
                update={
                    "action": "insufficient_evidence",
                    "stop_reason": stop_reason or "max_rounds",
                    "missing_info": list(last_decision.missing_info) + ["轮次用尽，停止继续派发"],
                }
            )
        stop_reason = last_decision.stop_reason or stop_reason

    _finalize_run_status(live_journal, diagnosis_task_id, last_decision, stop_reason)
    structured = _finalize_structured(last_decision, session, stop_reason)
    long_term, message = diagnosis_conclusion(structured, structured.get("conclusion") or "")
    main_transcript.record(
        "diagnosis/end",
        "diagnosis_end",
        {"stop_reason": stop_reason, "fail_kind": structured.get("fail_kind")},
    )
    return DiagnosisResult(
        structured=structured,
        analysis_text=long_term,
        message=message,
        strategy="component_parallel",
        context_text=context_text,
        selected_context_ids=selected_ids,
        context_chars=context_chars or len(context_text),
        latency_ms=int((time.perf_counter() - started) * 1000),
        token_usage=usage.as_dict(),
        tool_calls=budget.used_tool_calls,
        tool_trace=session.tool_trace,
        budget_used=budget.used_tool_calls,
        obs_compressed_n=len(session.evidence),
        ruled_out_n=len(structured.get("ruled_out") or []),
        react_limit=budget.max_rounds,
        react_context_chars=react_context_chars,
        archived_n=len(main_transcript.refs),
        transcript_event_n=len(main_transcript.events(limit=1000)),
    )


def _bind_main_transcript(
    *, user_id: str, task_id: str, injected: Transcript | None
) -> Transcript:
    if injected is not None:
        return injected
    settings = get_settings()
    scope = TranscriptScope(
        user_id=_scope_id(user_id, "anonymous"),
        task_id=_scope_id(task_id, "adhoc"),
        agent_id="diagnose-main",
        execution_id=uuid.uuid4().hex,
    )
    store: MemoryTranscriptStore | PostgresTranscriptStore
    if (settings.postgres_dsn or "").strip():
        store = PostgresTranscriptStore()
    else:
        store = MemoryTranscriptStore()
    return Transcript(store, scope)


def _scope_id(value: str, default: str) -> str:
    return (value or "").strip() or default


def _first_pipeline_id(pipelines: Sequence[Mapping[str, Any]]) -> str:
    for item in pipelines:
        pid = str(item.get("pipeline_id") or "").strip()
        if pid:
            return pid
    raise ValueError("并行组件诊断需要 pipeline_id")


def _version_tag(pipelines: Sequence[Mapping[str, Any]]) -> str:
    for item in pipelines:
        version = str(item.get("version") or "").strip()
        if version:
            return version
    return "missing"


def _init_overview(
    pipeline_id: str,
    *,
    scenario: MockScenario,
    transcript: Transcript,
    on_tool_start: Callable[[], None] | None = None,
) -> tuple[str, int, list[dict[str, Any]]]:
    """确定性初始化：目录、状态，再有界粗扫 failure_cues；每次真正 invoke 前由回调记账。"""
    tools = {
        t.name: t
        for t in build_diagnose_tools(
            scenario=scenario,
            budget=CharBudget(limit=40_000),
            archive=transcript,
            transcript=transcript,
        )
    }
    trace: list[dict[str, Any]] = []
    calls = 0
    chunks: list[str] = []
    for name, args in (
        ("list_log_files", {"pipeline_id": pipeline_id}),
        ("get_pipeline_status", {"pipeline_id": pipeline_id}),
    ):
        tool = tools.get(name)
        if tool is None:
            continue
        trace.append({"type": "call", "name": name, "args_preview": json.dumps(args, ensure_ascii=False)})
        if on_tool_start is not None:
            on_tool_start()
        try:
            text = str(tool.invoke(args))
            status = "success"
        except Exception as exc:  # noqa: BLE001
            text = f"[{name} error] {exc}"
            status = "error"
        calls += 1
        trace.append({"type": "result", "name": name, "content_chars": len(text), "status": status})
        chunks.append(f"## {name}\n{text}")
    cues_text, cue_calls, cue_trace = scan_failure_cues(
        pipeline_id,
        tools=tools,
        on_tool_start=on_tool_start,
    )
    calls += cue_calls
    trace.extend(cue_trace)
    chunks.append(cues_text)
    return "\n\n".join(chunks), calls, trace


def _bind_journal(
    injected: InvestigationJournal | None,
    store: MemoryTranscriptStore | PostgresTranscriptStore,
) -> InvestigationJournal:
    """Memory transcript 必须配 Memory journal，即使环境里有 POSTGRES_DSN。"""
    if injected is not None:
        return injected
    if isinstance(store, MemoryTranscriptStore):
        return MemoryInvestigationJournal()
    if (get_settings().postgres_dsn or "").strip():
        return PostgresInvestigationJournal()
    return MemoryInvestigationJournal()


def _sync_budget(budget: DiagnosisBudget, journal: InvestigationJournal, run_id: str) -> None:
    rec = journal.get_run(run_id)
    if rec is not None:
        budget.used_tool_calls = rec.used_tool_calls


def _finalize_run_status(
    journal: InvestigationJournal,
    run_id: str,
    decision: MainDecision,
    stop_reason: str,
) -> None:
    if decision.action == "conclude":
        status = "succeeded"
    elif decision.action == "insufficient_evidence" or stop_reason:
        status = "insufficient"
    else:
        status = "failed"
    journal.update_run(run_id, status=status)


def _component_transcript(
    store: MemoryTranscriptStore | PostgresTranscriptStore,
    main_scope: TranscriptScope,
    component: str,
    execution_id: str,
) -> Transcript:
    return Transcript(
        store,
        TranscriptScope(
            user_id=main_scope.user_id,
            task_id=main_scope.task_id,
            agent_id=f"component-{component}",
            execution_id=execution_id,
        ),
    )


def _persist_result(
    store: MemoryTranscriptStore | PostgresTranscriptStore,
    main_scope: TranscriptScope,
    rec: InvestigationTaskRecord,
    report: ComponentReport,
    evidences: Sequence[Evidence],
) -> str:
    transcript = _component_transcript(store, main_scope, rec.component, rec.execution_id)
    ref = transcript.put_json(
        {
            "report": report.model_dump(mode="json"),
            "evidences": [item.model_dump(mode="json") for item in evidences],
        }
    )
    return ref.artifact_id


def _replay_result(
    store: MemoryTranscriptStore | PostgresTranscriptStore,
    main_scope: TranscriptScope,
    rec: InvestigationTaskRecord,
) -> tuple[ComponentReport, list[Evidence], TokenUsage, list[dict[str, Any]]]:
    transcript = _component_transcript(store, main_scope, rec.component, rec.execution_id)
    payload = transcript.read_json(rec.result_ref)
    report = ComponentReport.model_validate(payload["report"])
    evidences = [Evidence.model_validate(item) for item in payload.get("evidences") or []]
    return report, evidences, TokenUsage(), [{"type": "replay", "name": "result_ref"}]


def _task_from_record(rec: InvestigationTaskRecord, budget: DiagnosisBudget) -> InvestigationTask:
    scope = LogScope.model_validate(rec.log_scope)
    return InvestigationTask(
        investigation_id=rec.investigation_id,
        diagnosis_task_id=rec.run_id,
        round_index=rec.round_index,
        component=rec.component,
        question=rec.question,
        pipeline_id=scope.pipeline_id,
        log_scope=scope,
        budget=InvestigationBudget(
            max_steps=budget.component_max_steps,
            timeout_seconds=budget.component_timeout_seconds,
        ),
        execution_id=rec.execution_id,
        owner_token=rec.owner_token,
    )


def _succeeded_ids_by_fingerprint(journal: InvestigationJournal, run_id: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for rec in journal.list_tasks(run_id):
        if rec.status != "SUCCEEDED":
            continue
        try:
            task = _task_from_record(rec, DiagnosisBudget())
        except Exception:  # noqa: BLE001
            continue
        out[task.fingerprint()] = rec.investigation_id
    return out


def _timeout_report(task: InvestigationTask, *, findings: str) -> ComponentReport:
    return ComponentReport(
        investigation_id=task.investigation_id,
        component=task.component,
        status="timeout",
        findings=[findings],
        coverage=f"pipeline={task.pipeline_id} component={task.component}",
        missing=["调查未能在租约内完成"],
    )


def _wrap_journal_runner(
    inner: InvestigateFn,
    *,
    journal: InvestigationJournal,
    store: MemoryTranscriptStore | PostgresTranscriptStore,
    main_scope: TranscriptScope,
) -> InvestigateFn:
    def _run(task: InvestigationTask) -> tuple[ComponentReport, list[Evidence], TokenUsage, list[dict[str, Any]]]:
        rec = journal.get_task(task.investigation_id)
        if rec is None:
            rec = journal.persist_pending(
                investigation_id=task.investigation_id,
                run_id=task.diagnosis_task_id,
                round_index=task.round_index,
                component=task.component,
                question=task.question,
                log_scope=task.log_scope.model_dump(mode="json"),
            )
        if rec.status == "SUCCEEDED" and rec.result_ref:
            return _replay_result(store, main_scope, rec)
        try:
            rec = journal.claim(task.investigation_id, timeout_seconds=task.budget.timeout_seconds)
        except InvestigationInProgress:
            return (
                _timeout_report(task, findings="调查仍被其他租约占用"),
                [],
                TokenUsage(),
                [],
            )
        if rec.status == "SUCCEEDED" and rec.result_ref:
            return _replay_result(store, main_scope, rec)
        live = task.model_copy(update={"execution_id": rec.execution_id, "owner_token": rec.owner_token})
        try:
            report, evidences, usage, trace = inner(live)
        except Exception:
            journal.finish_failed(rec.investigation_id, rec.owner_token, "exception")
            raise
        if report.status == "timeout":
            expired = journal.expire_owned(rec.investigation_id, rec.owner_token)
            if not expired:
                latest = journal.get_task(rec.investigation_id)
                if latest is not None and latest.status == "SUCCEEDED" and latest.result_ref:
                    return _replay_result(store, main_scope, latest)
            return report, evidences, usage, trace
        try:
            result_ref = _persist_result(store, main_scope, rec, report, evidences)
        except Exception:
            journal.finish_failed(rec.investigation_id, rec.owner_token, "result_ref")
            raise
        if not journal.finish_succeeded(
            rec.investigation_id, rec.owner_token, result_ref
        ):
            latest = journal.get_task(rec.investigation_id)
            if latest is not None and latest.status == "SUCCEEDED" and latest.result_ref:
                return _replay_result(store, main_scope, latest)
        return report, evidences, usage, trace

    return _run


def _restore_from_journal(
    *,
    journal: InvestigationJournal,
    run_id: str,
    store: MemoryTranscriptStore | PostgresTranscriptStore,
    main_scope: TranscriptScope,
    session: DiagnosisSession,
    budget: DiagnosisBudget,
    runner: InvestigateFn,
) -> TokenUsage:
    """回放 SUCCEEDED，只重跑 PENDING / EXPIRED。"""
    journal.expire_stale(run_id)
    incomplete: list[InvestigationTask] = []
    usage = TokenUsage()
    for rec in journal.list_tasks(run_id):
        if rec.status == "SUCCEEDED" and rec.result_ref:
            try:
                report, evidences, _usage, _trace = _replay_result(store, main_scope, rec)
            except Exception:  # noqa: BLE001
                continue
            session.reports[rec.investigation_id] = report
            session.tasks[rec.investigation_id] = _task_from_record(rec, budget)
            for item in evidences:
                session.evidence[item.evidence_id] = item
        elif rec.status in {"PENDING", "EXPIRED"}:
            incomplete.append(_task_from_record(rec, budget))
    if not incomplete:
        return usage
    results = run_parallel_investigations(incomplete, runner, max_workers=budget.max_workers)
    for (report, evidences, inv_usage, trace), task in zip(results, incomplete):
        usage = usage + inv_usage
        session.reports[report.investigation_id] = report
        session.tasks[task.investigation_id] = task
        session.tool_trace.extend(trace)
        for item in evidences:
            session.evidence[item.evidence_id] = item
    return usage


def _make_default_runner(
    *,
    store: MemoryTranscriptStore | PostgresTranscriptStore,
    main_scope: TranscriptScope,
    scenario: MockScenario,
    user_id: str,
    on_tool_start: Callable[[], None] | None = None,
) -> InvestigateFn:
    def _run(task: InvestigationTask) -> tuple[ComponentReport, list[Evidence], TokenUsage, list[dict[str, Any]]]:
        scope = TranscriptScope(
            user_id=main_scope.user_id,
            task_id=main_scope.task_id,
            agent_id=f"component-{task.component}",
            execution_id=(task.execution_id or "").strip() or task.investigation_id,
        )
        return run_component_investigation(
            task,
            scenario=scenario,
            user_id=user_id,
            transcript=Transcript(store, scope),
            on_tool_start=on_tool_start,
        )

    return _run


def _make_llm_decide(model: BaseChatModel) -> DecideFn:
    def _decide(snapshot: dict[str, Any]) -> MainDecision:
        structured = model.with_structured_output(
            MainDecision, include_raw=True, method="function_calling"
        )
        raw = structured.invoke(
            [
                SystemMessage(content=_ORCHESTRATOR_SYSTEM),
                HumanMessage(content=json.dumps(snapshot, ensure_ascii=False, indent=2)),
            ]
        )
        usage = TokenUsage()
        if isinstance(raw, dict):
            usage = usage_from_message(raw.get("raw"))
            parsed = raw.get("parsed")
            if parsed is None:
                raise ValueError("主 ReAct 未返回 parsed")
            decision = parsed if isinstance(parsed, MainDecision) else MainDecision.model_validate(parsed)
        else:
            decision = raw if isinstance(raw, MainDecision) else MainDecision.model_validate(raw)
        object.__setattr__(decision, "_token_usage", usage)
        return decision

    return _decide


def _call_decide(decide: DecideFn, snapshot: dict[str, Any]) -> tuple[MainDecision, TokenUsage]:
    try:
        decision = decide(snapshot)
        usage = getattr(decision, "_token_usage", TokenUsage())
        if not isinstance(usage, TokenUsage):
            usage = TokenUsage()
        return decision, usage
    except Exception as exc:  # noqa: BLE001
        return (
            MainDecision(
                action="insufficient_evidence",
                stop_reason="decide_error",
                missing_info=[f"主 ReAct 决策失败: {exc}"],
            ),
            TokenUsage(),
        )


def _sanitize_decision(decision: MainDecision, session: DiagnosisSession) -> MainDecision:
    """拒绝未登记证据引用；调查任务最多 3 条。"""
    known = set(session.evidence)
    resolved = [eid for eid in decision.evidence_ids if eid in known]
    rejected = [eid for eid in decision.evidence_ids if eid not in known]
    updates: dict[str, Any] = {"evidence_ids": resolved}
    if decision.action == "investigate":
        updates["investigations"] = decision.investigations[:3]
    if rejected:
        missing = list(decision.missing_info) + [f"拒绝未登记证据引用: {rejected}"]
        updates["missing_info"] = missing
        if decision.action == "conclude" and not resolved:
            updates.update(
                {
                    "action": "insufficient_evidence",
                    "fail_kind": "unknown",
                    "stop_reason": "invalid_evidence_refs",
                    "root_cause": "unknown",
                }
            )
        elif decision.action == "conclude":
            updates["stop_reason"] = decision.stop_reason or "filtered_evidence_refs"
    return decision.model_copy(update=updates)


def _merge_hypotheses(session: DiagnosisSession, incoming: Sequence[Hypothesis]) -> None:
    for item in incoming:
        session.hypotheses[item.hypothesis_id] = item


def _compose_main_context(
    *,
    user_input: str,
    overview: str,
    session: DiagnosisSession,
    budget: DiagnosisBudget,
    round_index: int,
    transcript: Transcript,
    compressor: Any,
) -> tuple[str, list[str], int, list[str]]:
    """主上下文只含假设、报告摘要、证据引用、反证与缺口。"""
    from work_agent.graph.helpers.context_compressor import ContextCompressor

    items: list[ContextItem] = []
    goal = clip_text((user_input or "").strip(), max_chars=_GOAL_MAX_CHARS)
    if goal:
        items.append(
            ContextItem(
                item_id="goal",
                kind="goal",
                source="user",
                text=goal,
                priority=PRIORITY_CONCLUSION,
                pin=PIN_IMMUTABLE,
            )
        )
    items.append(
        ContextItem(
            item_id="budget",
            kind="note",
            source="runtime",
            text=(
                f"round={round_index}/{budget.max_rounds} "
                f"tools_left={budget.remaining_tools()} "
                f"seconds_left={int(budget.remaining_seconds())}"
            ),
            priority=PRIORITY_CONCLUSION,
            pin=PIN_PROTECTED,
        )
    )
    hyp_text = json.dumps(
        [item.model_dump() for item in session.hypotheses.values()],
        ensure_ascii=False,
        indent=2,
    )
    items.append(
        ContextItem(
            item_id="hypotheses",
            kind="note",
            source="hypotheses",
            text=hyp_text if session.hypotheses else "（尚无假设）",
            priority=PRIORITY_CONCLUSION,
            pin=PIN_PROTECTED,
        )
    )
    contradictions = collect_contradictions(session.report_list(), list(session.hypotheses.values()))
    items.append(
        ContextItem(
            item_id="contradictions",
            kind="ruled_out",
            source="aggregator",
            text="\n".join(contradictions) if contradictions else "（无矛盾）",
            priority=PRIORITY_RULED_OUT,
            pin=PIN_PROTECTED,
        )
    )
    gaps = _collect_gaps(session)
    items.append(
        ContextItem(
            item_id="gaps",
            kind="note",
            source="gaps",
            text="\n".join(gaps) if gaps else "（无额外缺口）",
            priority=PRIORITY_RULED_OUT,
            pin=PIN_PROTECTED,
        )
    )
    ref_lines = [
        f"{item.evidence_id} [{item.source}/{item.component}] artifact={item.artifact_id} excerpt={item.excerpt[:180]}"
        for item in session.evidence_list()
    ]
    items.append(
        ContextItem(
            item_id="evidence_refs",
            kind="evidence",
            source="evidence",
            text="\n".join(ref_lines) if ref_lines else "（尚无证据引用）",
            priority=PRIORITY_EVIDENCE,
            pin=PIN_PROTECTED,
        )
    )
    overview_clip = clip_text(overview, max_chars=3000)
    items.append(
        ContextItem(
            item_id="overview",
            kind="note",
            source="list_log_files",
            text=overview_clip,
            priority=PRIORITY_EVIDENCE,
            pin=PIN_NORMAL,
        )
    )
    for report in session.report_list():
        items.append(
            ContextItem(
                item_id=f"report-{report.investigation_id[:8]}-{report.component}",
                kind="tool_result",
                source=report.component,
                text=json.dumps(report.model_dump(), ensure_ascii=False, indent=2),
                priority=PRIORITY_EVIDENCE,
                pin=PIN_NORMAL,
            )
        )
    manager = ContextManager(
        compressor=compressor if compressor is not None else ContextCompressor(),
        archive=transcript,
    )
    limit = get_settings().profile.react_history_max_chars or 20_000
    rendered = manager.render(items, goal=user_input or overview_clip, limit=limit)
    protected = ["hypotheses", "contradictions", "gaps", "evidence_refs", "budget"]
    if goal:
        protected.append("goal")
    return rendered.text, rendered.selected_ids, rendered.context_chars, protected


def _collect_gaps(session: DiagnosisSession) -> list[str]:
    gaps: list[str] = list(session.skipped)
    for report in session.report_list():
        for item in report.missing:
            gaps.append(f"{report.component}: {item}")
        if report.status in {"timeout", "tool_error", "no_hit"}:
            gaps.append(f"{report.component} status={report.status} coverage={report.coverage}")
    return gaps


def _decision_snapshot(
    *,
    user_input: str,
    overview: str,
    session: DiagnosisSession,
    budget: DiagnosisBudget,
    round_index: int,
    context_text: str,
) -> dict[str, Any]:
    return {
        "user_input": user_input,
        "round": round_index,
        "tools_left": budget.remaining_tools(),
        "seconds_left": int(budget.remaining_seconds()),
        "known_evidence_ids": [item.evidence_id for item in session.evidence_list()],
        "reports": [item.model_dump() for item in session.report_list()],
        "hypotheses": [item.model_dump() for item in session.hypotheses.values()],
        "contradictions": collect_contradictions(
            session.report_list(), list(session.hypotheses.values())
        ),
        "gaps": _collect_gaps(session),
        "overview": overview[:4000],
        "working_context": context_text,
    }


def _finalize_structured(
    decision: MainDecision, session: DiagnosisSession, stop_reason: str
) -> dict[str, Any]:
    evidence_items = session.evidence_list()
    refs = list(decision.evidence_ids)
    if not refs:
        refs = [item.evidence_id for item in evidence_items if item.source == "log"][:8]
    excerpts = []
    for eid in refs:
        item = session.evidence.get(eid)
        if item is not None and item.excerpt:
            excerpts.append(item.excerpt)
    evidence_text = decision.evidence.strip() or "\n".join(excerpts)
    findings = [
        f"{report.component}[{report.status}]: " + "；".join(report.findings[:3])
        for report in session.report_list()
    ]
    hypotheses = [item.description for item in session.hypotheses.values()]
    if decision.hypotheses:
        hypotheses = [item.description for item in decision.hypotheses]
    fail_kind = decision.fail_kind or "unknown"
    if decision.action == "insufficient_evidence" and fail_kind not in {"unknown", "none"}:
        # 资料不足时仍可保留候选类型，但根因不足则标 unknown
        if not refs:
            fail_kind = "unknown"
    return {
        "fail_kind": fail_kind,
        "root_component": decision.root_component or "unknown",
        "root_cause": decision.root_cause or "unknown",
        "evidence": evidence_text,
        "conclusion": decision.conclusion
        or ("证据不足，仅给出候选与缺失" if decision.action != "conclude" else ""),
        "suggestion": decision.suggestion or "请根据缺失项补查后再归因",
        "ruled_out": decision.ruled_out or [],
        "evidence_refs": refs,
        "component_findings": findings,
        "candidate_hypotheses": hypotheses,
        "stop_reason": decision.stop_reason or stop_reason,
        "missing_info": list(decision.missing_info),
    }
