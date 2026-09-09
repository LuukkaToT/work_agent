"""
组件 Agent：对一条 InvestigationTask 跑范围锁定的 ReAct。

独立 transcript（agent_id=component-{id}，execution_id 必须是本 attempt 的新 ID）。
已知工具失败记为观察；程序异常上抛。超时先 cancel Deadline，产出缺失报告，不拖死整轮。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from work_agent.core.components import get_component, load_component_skill
from work_agent.core.llm import get_fast_model, get_reasoning_model
from work_agent.core.transcript import Transcript
from work_agent.core.usage import TokenUsage, usage_from_message
from work_agent.graph.helpers.agent_loop import run_agent_loop
from work_agent.graph.helpers.deadline import Deadline, DeadlineExceeded
from work_agent.graph.helpers.diagnose_tools import build_scoped_diagnose_tools
from work_agent.graph.helpers.diagnosis_models import (
    ComponentReport,
    Evidence,
    InvestigationTask,
    ReportStatus,
)
from work_agent.graph.helpers.truncate import CharBudget
from work_agent.tools.mock.scenarios import MockScenario

_EXTRACT_SYSTEM = (
    "根据本组件调查的工具观察抽取结构化报告。"
    "status 只能是 ok / timeout / tool_error / no_hit。"
    "无命中不得写组件正常；知识检索不能当主证。"
    "evidence_ids 只能使用已给出的证据 ID，禁止编造。"
    "跨组件事项写入 suggested_followups。"
)


class ComponentReportDraft(BaseModel):
    """快模型抽取稿；运行时再填调查 ID 并校验证据引用。"""

    status: ReportStatus = "no_hit"
    findings: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    supports_hypotheses: list[str] = Field(default_factory=list)
    contradicts_hypotheses: list[str] = Field(default_factory=list)
    coverage: str = ""
    missing: list[str] = Field(default_factory=list)
    suggested_followups: list[str] = Field(default_factory=list)


def run_component_investigation(
    task: InvestigationTask,
    *,
    scenario: MockScenario = "case_error",
    user_id: str = "",
    transcript: Transcript,
    model: BaseChatModel | None = None,
    extract_model: BaseChatModel | None = None,
    loop_fn: Callable[..., Any] | None = None,
    deadline: Deadline | None = None,
    on_tool_start: Callable[[], None] | None = None,
) -> tuple[ComponentReport, list[Evidence], TokenUsage, list[dict[str, Any]]]:
    """
    跑一次组件调查。

    参数:
        task: 派发前已生成 ID、已锁定范围的任务。
        scenario: mock 故障场景。
        user_id: 工号，仅透传给只读工具。
        transcript: 本调查独立历史流，必须已经绑定本 attempt 的 execution_id。
        model / extract_model: 注入用；None 分别用推理模型与快模型。
        loop_fn: 注入 ``run_agent_loop``；单测可替换。
        deadline: 可选截止；None 时按任务超时新建。父线程超时先 cancel 再返回。
        on_tool_start: 透传给循环；白名单工具真正发起前记账。

    返回:
        (报告, 证据列表, token 用量, 工具轨迹)。
    """
    started = time.perf_counter()
    live_deadline = deadline or Deadline(task.budget.timeout_seconds)
    spec = get_component(task.component)
    budget = CharBudget(limit=40_000)
    tools = build_scoped_diagnose_tools(
        pipeline_id=task.pipeline_id,
        component=task.component,
        allowed_names=spec.tools,
        scenario=scenario,
        budget=budget,
        tool_result_max_chars=task.budget.tool_result_max_chars,
        user_id=user_id,
        archive=transcript,
        transcript=transcript,
    )
    system = load_component_skill(task.component)
    user = _component_user_prompt(task)
    react_model = model or get_reasoning_model(temperature=0)
    runner = loop_fn or run_agent_loop

    def _body() -> tuple[Any, TokenUsage]:
        """在工作线程里跑 ReAct 循环，把 loop 与用量一并带回。"""
        loop = _call_loop(
            runner,
            model=react_model,
            tools=tools,
            system=system,
            user=user,
            max_steps=task.budget.max_steps,
            observation_max_chars=task.budget.tool_result_max_chars,
            history_max_chars=task.budget.history_max_chars,
            transcript=transcript,
            deadline=live_deadline,
            on_tool_start=on_tool_start,
        )
        return loop, loop.usage

    usage = TokenUsage()
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_body)
    try:
        loop, loop_usage = future.result(timeout=task.budget.timeout_seconds)
        usage = usage + loop_usage
    except (FuturesTimeout, DeadlineExceeded):
        live_deadline.cancel()
        pool.shutdown(wait=False)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        report = ComponentReport(
            investigation_id=task.investigation_id,
            component=task.component,
            status="timeout",
            findings=["组件调查超时，本轮结果不完整"],
            coverage=_coverage(task),
            missing=["超时前未完成的取证步骤"],
            elapsed_ms=elapsed_ms,
        )
        return report, [], usage, []
    else:
        pool.shutdown(wait=True)

    from work_agent.graph.nodes.error_analysis import extract_tool_trace

    tool_trace = extract_tool_trace(loop.messages)
    tool_calls = sum(1 for item in tool_trace if item.get("type") == "call")
    evidences = collect_evidence(task, loop.messages, transcript)
    report, extract_usage = extract_component_report(
        task,
        messages=loop.messages,
        evidences=evidences,
        tool_calls=tool_calls,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        extract_model=extract_model,
        deadline=live_deadline,
    )
    usage = usage + extract_usage
    transcript.put_json(report.model_dump())
    return report, evidences, usage, tool_trace


def _call_loop(runner: Callable[..., Any], **kwargs: Any) -> Any:
    """
    调用 ``run_agent_loop``（或测试替身）。

    参数:
        runner: 循环函数。
        **kwargs: 透传参数；``deadline`` / ``on_tool_start`` 若被旧替身拒绝则去掉再试。

    返回:
        runner 的返回值。
    """
    optional = ("deadline", "on_tool_start")
    try:
        return runner(**kwargs)
    except TypeError as exc:
        message = str(exc)
        dropped = False
        for name in optional:
            if name in kwargs and name in message:
                kwargs.pop(name, None)
                dropped = True
        if dropped:
            return _call_loop(runner, **kwargs)
        raise


def collect_evidence(
    task: InvestigationTask,
    messages: list,
    transcript: Transcript,
) -> list[Evidence]:
    """
    从 ToolMessage 采集证据：原文先 put_text，报告只留引用。

    grep/fetch 记为 log；lookup/search_knowledge 记为 knowledge。
    工具错误与越界拒绝不作为证据。

    参数:
        task: 当前调查任务。
        messages: ReAct 循环产出的消息。
        transcript: 本调查独立归档。

    返回:
        已落盘引用的证据列表。
    """
    out: list[Evidence] = []
    seq = 0
    for msg in messages or []:
        if not isinstance(msg, ToolMessage):
            continue
        name = getattr(msg, "name", "") or "tool"
        text = getattr(msg, "content", "") or ""
        if not isinstance(text, str):
            text = str(text)
        status = getattr(msg, "status", "success") or "success"
        if status != "success":
            continue
        if _is_tool_failure_text(text):
            continue
        source = _evidence_source(name)
        if source is None:
            continue
        seq += 1
        ref = transcript.put_text(text)
        excerpt = _excerpt(text)
        out.append(
            Evidence(
                evidence_id=f"ev-{task.investigation_id[:8]}-{seq:03d}",
                source=source,
                component=task.component,
                file=f"{task.component}.log" if source == "log" else "missing",
                line="missing",
                timestamp="missing",
                artifact_id=ref.artifact_id,
                excerpt=excerpt,
                collection_condition=(
                    f"pipeline_id={task.pipeline_id} component={task.component} "
                    f"tool={name} tail_lines={task.log_scope.tail_lines}"
                ),
            )
        )
    return out


def extract_component_report(
    task: InvestigationTask,
    *,
    messages: list,
    evidences: list[Evidence],
    tool_calls: int,
    elapsed_ms: int,
    extract_model: BaseChatModel | None = None,
    deadline: Deadline | None = None,
) -> tuple[ComponentReport, TokenUsage]:
    """
    快模型抽报告；证据 ID 以已落盘列表为准，模型不能编造引用。

    抽取失败或已过截止时退回规则报告。

    参数:
        task: 当前调查任务。
        messages: ReAct 消息，给抽取当观察。
        evidences: 已采集证据。
        tool_calls: 本调查工具调用次数。
        elapsed_ms: 调查耗时毫秒。
        extract_model: 注入用；None 用快模型。
        deadline: 抽取前再检查一次截止。

    返回:
        ``(报告, 抽取用量)``。
    """
    fallback = _fallback_report(task, messages, evidences, tool_calls, elapsed_ms)
    if deadline is not None:
        try:
            deadline.check()
        except DeadlineExceeded:
            return fallback, TokenUsage()
    observations = _observation_digest(messages, evidences)
    try:
        model = extract_model or get_fast_model(temperature=0)
        structured = model.with_structured_output(
            ComponentReportDraft, include_raw=True, method="function_calling"
        )
        from work_agent.graph.helpers.deadline import invoke_with_deadline

        raw = invoke_with_deadline(
            structured,
            [
                SystemMessage(content=_EXTRACT_SYSTEM),
                HumanMessage(content=observations),
            ],
            deadline,
        )
        if isinstance(raw, dict):
            usage = usage_from_message(raw.get("raw"))
            parsed = raw.get("parsed")
        else:
            usage = TokenUsage()
            parsed = raw
        if parsed is None:
            raise ValueError("组件报告抽取未返回 parsed")
        draft = (
            parsed
            if isinstance(parsed, ComponentReportDraft)
            else ComponentReportDraft.model_validate(parsed)
        )
    except Exception:  # noqa: BLE001
        return fallback, TokenUsage()

    allowed = {item.evidence_id for item in evidences}
    evidence_ids = [eid for eid in draft.evidence_ids if eid in allowed]
    if not evidence_ids:
        evidence_ids = [item.evidence_id for item in evidences if item.source == "log"]
    status = _coerce_status(draft.status, messages, evidences)
    return (
        ComponentReport(
            investigation_id=task.investigation_id,
            component=task.component,
            status=status,  # type: ignore[arg-type]
            findings=list(draft.findings),
            evidence_ids=evidence_ids,
            supports_hypotheses=list(draft.supports_hypotheses),
            contradicts_hypotheses=list(draft.contradicts_hypotheses),
            coverage=draft.coverage.strip() or _coverage(task),
            missing=list(draft.missing),
            suggested_followups=list(draft.suggested_followups),
            tool_calls=tool_calls,
            elapsed_ms=elapsed_ms,
        ),
        usage,
    )


def _component_user_prompt(task: InvestigationTask) -> str:
    """拼本调查的用户消息：任务 JSON + 范围锁定规则。"""
    import json

    payload = {
        "investigation_id": task.investigation_id,
        "component": task.component,
        "question": task.question,
        "pipeline_id": task.pipeline_id,
        "log_scope": task.log_scope.model_dump(),
        "related_hypotheses": task.related_hypothesis_ids,
        "rules": [
            "只能查本组件日志，越界参数会被拒绝",
            "无命中必须写覆盖范围，不得写组件正常",
            "知识检索仅旁证",
            "跨组件补查只写建议",
        ],
    }
    return (
        "请仅针对下列调查取证，完成后用中文给出发现与缺口。\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _coverage(task: InvestigationTask) -> str:
    """把 log_scope 收成报告里的覆盖范围一句。"""
    scope = task.log_scope
    return (
        f"pipeline={scope.pipeline_id} component={scope.component}.log "
        f"tail_lines={scope.tail_lines if scope.tail_lines is not None else 'missing'} "
        f"start_ts={scope.start_ts} end_ts={scope.end_ts} version={scope.version_tag or 'missing'}"
    )


def _evidence_source(tool_name: str) -> str | None:
    """
    工具名映射到证据来源。

    返回:
        ``log`` / ``knowledge``；未登记的工具返回 ``None``（不采证）。
    """
    if tool_name in {"fetch_logs", "grep_logs"}:
        return "log"
    if tool_name in {"search_knowledge", "lookup_error_code"}:
        return "knowledge"
    return None


def _is_tool_failure_text(text: str) -> bool:
    """判断工具正文是否是错误/越界拒绝，这类不当证据。"""
    head = text.lstrip()
    return head.startswith("[") and (
        "error]" in head[:80].casefold()
        or "scope_rejected" in head[:80].casefold()
        or "越界" in head[:80]
        or "unknown tool" in head[:80].casefold()
    )


def _excerpt(text: str, limit: int = 400) -> str:
    """压空白后截断；超出 ``limit`` 时末尾加省略号。"""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _observation_digest(messages: list, evidences: list[Evidence]) -> str:
    """把工具观察与已落盘证据收成抽取模型的用户输入 JSON。"""
    import json

    lines: list[str] = []
    for msg in messages or []:
        if not isinstance(msg, ToolMessage):
            continue
        name = getattr(msg, "name", "") or "tool"
        text = getattr(msg, "content", "") or ""
        if not isinstance(text, str):
            text = str(text)
        lines.append(f"{name}: {_excerpt(text, 800)}")
    payload = {
        "observations": lines,
        "evidence": [item.model_dump() for item in evidences],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _fallback_report(
    task: InvestigationTask,
    messages: list,
    evidences: list[Evidence],
    tool_calls: int,
    elapsed_ms: int,
) -> ComponentReport:
    """抽取失败时按日志命中/工具错误给出规则报告，避免空 status。"""
    log_hits = [item for item in evidences if item.source == "log"]
    tool_error = _had_tool_error(messages)
    if tool_error and not log_hits:
        status: str = "tool_error"
        findings = ["工具失败，未能完成本组件取证"]
        missing = ["成功的本组件日志命中"]
    elif not log_hits:
        status = "no_hit"
        findings = ["指定范围内未命中相关日志，不能据此判定组件正常"]
        missing = ["指向故障的本组件日志原文"]
    else:
        status = "ok"
        findings = [item.excerpt[:200] for item in log_hits[:5]]
        missing = []
    return ComponentReport(
        investigation_id=task.investigation_id,
        component=task.component,
        status=status,  # type: ignore[arg-type]
        findings=findings,
        evidence_ids=[item.evidence_id for item in log_hits],
        coverage=_coverage(task),
        missing=missing,
        tool_calls=tool_calls,
        elapsed_ms=elapsed_ms,
    )


def _had_tool_error(messages: list) -> bool:
    """消息里是否出现工具 error 状态或失败正文。"""
    for msg in messages or []:
        if not isinstance(msg, ToolMessage):
            continue
        if (getattr(msg, "status", "success") or "success") == "error":
            return True
        text = getattr(msg, "content", "") or ""
        if isinstance(text, str) and _is_tool_failure_text(text):
            return True
    return False


def _coerce_status(status: str, messages: list, evidences: list[Evidence]) -> str:
    """
    用证据覆盖模型给的 status：无日志命中不能写 ok；有命中不能写 no_hit。

    参数:
        status: 抽取稿上的状态。
        messages: ReAct 消息，用于判断工具失败。
        evidences: 已采集证据。

    返回:
        ``ok`` / ``timeout`` / ``tool_error`` / ``no_hit`` 之一。
    """
    log_hits = [item for item in evidences if item.source == "log"]
    if status == "timeout":
        return "timeout"
    if _had_tool_error(messages) and not log_hits:
        return "tool_error"
    if not log_hits:
        return "no_hit"
    if status not in {"ok", "timeout", "tool_error", "no_hit"}:
        return "ok"
    if status == "no_hit" and log_hits:
        return "ok"
    return status
