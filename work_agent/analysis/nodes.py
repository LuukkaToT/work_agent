"""测试分析主子图节点。"""

from __future__ import annotations

from collections import defaultdict
import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from work_agent.analysis.artifacts import ArtifactStore
from work_agent.analysis.corpus import (
    get_knowledge_retriever,
    normalize_channel,
)
from work_agent.analysis.coverage import gaps_by_task, review_coverage
from work_agent.analysis.llm import invoke_structured
from work_agent.analysis.prompts import load_prompt
from work_agent.analysis.report import render_markdown
from work_agent.analysis.research import build_domain_research_graph
from work_agent.analysis.schemas import (
    AnalysisOutput,
    AnalysisPlan,
    CoverageGap,
    DEFAULT_SCENARIO_TYPES,
    DomainAnalysisResult,
    DomainTask,
    RequirementFact,
    ScenarioType,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def artifact_store_for_task(task_id: str) -> ArtifactStore:
    """统一创建当前任务的分析产物存储，方便测试替换入口。"""

    return ArtifactStore.for_task(task_id)


def initialize_analysis(state: dict) -> dict:
    """校验最小输入，并重置一次测试分析运行的私有状态。"""

    user_input = str(state.get("user_input") or "").strip()
    if not user_input:
        raise ValueError("测试分析需要需求文本")
    return {
        "requirement_ref": "",
        "plan_ref": "",
        "domain_result_refs": {},
        "domain_summaries": {},
        "coverage_ref": "",
        "coverage_gaps": [],
        "repair_count": 0,
        "analysis_path": "",
        "summary": {},
        "audit": [
            {
                "step": "analysis_initialize",
                "task_id": state.get("task_id"),
            }
        ],
    }


def _fallback_requirement(user_input: str, available_channels: list[str]) -> RequirementFact:
    """Structured Output 不可用时，用保守规则提取可继续执行的最低事实。"""

    channels = [
        channel
        for channel in available_channels
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(channel)}(?![A-Za-z0-9])", user_input, re.I)
    ]
    upper = user_input.upper()
    directions = []
    if any(term in upper for term in ["上行", "UL", "UPLINK"]):
        directions.append("UL")
    if any(term in upper for term in ["下行", "DL", "DOWNLINK"]):
        directions.append("DL")
    if not directions:
        directions.append("NA")
    return RequirementFact(
        title=user_input[:60],
        summary=user_input,
        raw_requirement=user_input,
        sufficient=len(user_input) >= 8,
        directions=directions,  # type: ignore[arg-type]
        channels=channels,
        missing_information=["产品版本、组网与参数范围待确认"],
        retrieval_terms=[user_input, *channels],
    )


def extract_requirement(state: dict) -> dict:
    """把原始需求结构化，并持久化为后续节点唯一读取的事实快照。"""

    user_input = str(state["user_input"]).strip()
    retriever = get_knowledge_retriever()
    available_channels = retriever.available_channels()
    try:
        fact = invoke_structured(
            RequirementFact,
            [
                SystemMessage(content=load_prompt("extract_requirement")),
                HumanMessage(
                    content=(
                        f"【用户需求】\n{user_input}\n\n"
                        f"【资料库可用信道】\n{_json(available_channels)}"
                    )
                ),
            ],
        )
    except Exception:
        # 模型或协议失败不应让整张图直接中断；兜底结果会显式保留待确认项。
        fact = _fallback_requirement(user_input, available_channels)

    # 只保留资料库支持的标准信道，防止模型生成任意目录名。
    normalized_channels = []
    for raw in fact.channels:
        channel = normalize_channel(raw)
        if channel and channel not in normalized_channels:
            normalized_channels.append(channel)
    fact.channels = normalized_channels
    fact.raw_requirement = user_input
    if not fact.retrieval_terms:
        fact.retrieval_terms = [fact.title, *fact.features, *fact.procedures]

    store = artifact_store_for_task(str(state["task_id"]))
    ref = store.write_json("requirement.json", fact.model_dump(mode="json"))
    return {
        "requirement_ref": ref,
        "requirement": user_input,
        "audit": [
            {
                "step": "extract_requirement",
                "channels": fact.channels,
                "sufficient": fact.sufficient,
                "requirement_ref": ref,
            }
        ],
    }


def _fallback_plan(
    fact: RequirementFact,
    available_channels: list[str],
) -> AnalysisPlan:
    """规划模型不可用时，按已识别信道创建最小领域任务。"""

    def common_plan() -> AnalysisPlan:
        return AnalysisPlan(
            tasks=[
                DomainTask(
                    task_id="task_common_01",
                    name="通用测试分析",
                    domain="COMMON",
                    channels=[],
                    objectives=[
                        "分析需求的正常、边界、异常、重配置和恢复测试点"
                    ],
                    retrieval_queries=fact.retrieval_terms or [fact.summary],
                )
            ]
        )

    channels = fact.channels or []
    if not channels:
        # 未识别到信道时只建通用任务，不把整个信道库塞给模型。
        return common_plan()
    tasks = []
    for index, channel in enumerate(channels, 1):
        if channel not in available_channels:
            continue
        tasks.append(
            DomainTask(
                task_id=f"task_{channel.lower().replace('-', '_')}_{index:02d}",
                name=f"{channel} 信道测试分析",
                domain=channel,
                channels=[channel],
                objectives=[
                    f"分析 {channel} 相关功能影响、配置、异常与恢复测试点"
                ],
                retrieval_queries=[*fact.retrieval_terms, channel],
            )
        )
    return AnalysisPlan(tasks=tasks) if tasks else common_plan()


def _normalize_plan(
    plan: AnalysisPlan,
    fact: RequirementFact,
    available_channels: list[str],
) -> AnalysisPlan:
    """校验、补全并合并模型规划，建立稳定的执行边界。

    模型负责建议如何拆域；代码负责限制信道、任务 ID、任务数量和重复任务，
    确保后续 Tool 权限与产物路径不受自由文本控制。
    """

    normalized: list[DomainTask] = []
    seen_ids: set[str] = set()
    for index, task in enumerate(plan.tasks, 1):
        channels = []
        for raw in task.channels:
            channel = normalize_channel(raw)
            if channel and channel in available_channels and channel not in channels:
                channels.append(channel)
        if not channels and normalize_channel(task.domain) in available_channels:
            channels = [normalize_channel(task.domain)]  # type: ignore[list-item]

        task_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", task.task_id).strip("_")
        if not task_id or task_id in seen_ids:
            task_id = f"task_{index:02d}"
        seen_ids.add(task_id)
        task.task_id = task_id
        task.channels = channels
        task.domain = (
            channels[0]
            if len(channels) == 1
            else task.domain.strip() or "COMMON"
        )
        task.required_scenario_types = list(
            dict.fromkeys(task.required_scenario_types or DEFAULT_SCENARIO_TYPES)
        )
        task.retrieval_queries = list(
            dict.fromkeys(
                [
                    *(task.retrieval_queries or []),
                    *fact.retrieval_terms,
                    *channels,
                ]
            )
        )
        if not task.objectives:
            task.objectives = [f"完成 {task.domain} 的结构化测试分析"]
        normalized.append(task)
    if not normalized:
        return _fallback_plan(fact, available_channels)

    # 规划模型有时会按 normal/boundary/... 为同一信道创建多条任务。
    # 资料目录按信道组织，因此代码将相同信道集合合并成一个研究任务。
    merged: dict[tuple[str, ...] | tuple[str, str], DomainTask] = {}
    for task in normalized:
        key: tuple[str, ...] | tuple[str, str]
        key = tuple(task.channels) if task.channels else ("domain", task.domain)
        current = merged.get(key)
        if current is None:
            merged[key] = task
            continue
        current.objectives = list(
            dict.fromkeys([*current.objectives, *task.objectives])
        )
        current.required_scenario_types = list(
            dict.fromkeys(
                [
                    *current.required_scenario_types,
                    *task.required_scenario_types,
                ]
            )
        )
        current.retrieval_queries = list(
            dict.fromkeys([*current.retrieval_queries, *task.retrieval_queries])
        )
        current.name = (
            f"{current.channels[0]} 综合测试分析"
            if len(current.channels) == 1
            else current.name
        )
    # 限制领域数量，避免一次宽泛需求导致不受控的模型调用和资料读取。
    plan.tasks = list(merged.values())[:8]
    return plan


def plan_domains(state: dict) -> dict:
    """根据需求事实规划领域研究任务，并写入可审计计划文件。"""

    store = artifact_store_for_task(str(state["task_id"]))
    fact = RequirementFact.model_validate(store.read_json(state["requirement_ref"]))
    retriever = get_knowledge_retriever()
    available_channels = retriever.available_channels()
    try:
        plan = invoke_structured(
            AnalysisPlan,
            [
                SystemMessage(content=load_prompt("plan_domains")),
                HumanMessage(
                    content=(
                        f"【需求事实】\n{_json(fact.model_dump(mode='json'))}\n\n"
                        f"【可用信道资料目录】\n{_json(available_channels)}"
                    )
                ),
            ],
        )
    except Exception:
        plan = _fallback_plan(fact, available_channels)
    plan = _normalize_plan(plan, fact, available_channels)

    ref = store.write_json("domain_plan.json", plan.model_dump(mode="json"))
    return {
        "plan_ref": ref,
        "audit": [
            {
                "step": "plan_domains",
                "task_count": len(plan.tasks),
                "domains": [task.domain for task in plan.tasks],
                "plan_ref": ref,
            }
        ],
    }


def _failed_result(task: DomainTask, exc: Exception) -> DomainAnalysisResult:
    """把单领域异常转换为结构化失败，允许其他领域继续完成。"""

    return DomainAnalysisResult(
        task_id=task.task_id,
        domain=task.domain,
        channels=task.channels,
        status="failed",
        summary="领域分析执行失败",
        warnings=[re.sub(r"\s+", " ", str(exc)).strip()[:300]],
    )


def analyze_domains(state: dict) -> dict:
    """逐个执行隔离的 DomainResearch 子图并保存领域结果。

    当前顺序执行便于控制模型限流和产物写入；每个领域单独捕获异常，因此一个
    信道失败不会使其他信道的有效分析丢失。
    """

    store = artifact_store_for_task(str(state["task_id"]))
    fact_data = store.read_json(state["requirement_ref"])
    plan = AnalysisPlan.model_validate(store.read_json(state["plan_ref"]))
    retriever = get_knowledge_retriever()
    research_graph = build_domain_research_graph()

    refs: dict[str, str] = {}
    summaries: dict[str, dict] = {}
    audit: list[dict] = []
    for task in plan.tasks:
        # 白名单由代码根据任务和显式依赖生成，之后通过 State 注入检索 Tool。
        allowed_channels = retriever.expand_allowed_channels(task.channels)
        try:
            research_output = research_graph.invoke(
                {
                    "requirement_fact": fact_data,
                    "domain_task": task.model_dump(mode="json"),
                    "allowed_channels": allowed_channels,
                }
            )
            result = DomainAnalysisResult.model_validate(research_output["result"])
            audit.extend(research_output.get("audit") or [])
        except Exception as exc:
            result = _failed_result(task, exc)

        ref = store.write_json(
            f"domains/{task.task_id}.json",
            result.model_dump(mode="json"),
        )
        refs[task.task_id] = ref
        summaries[task.task_id] = {
            "domain": result.domain,
            "status": result.status,
            "scenario_count": len(result.scenarios),
            "evidence_count": len(result.evidence),
        }
        audit.append(
            {
                "step": "analyze_domain",
                "task_id": task.task_id,
                **summaries[task.task_id],
            }
        )
    return {
        "domain_result_refs": refs,
        "domain_summaries": summaries,
        "audit": audit,
    }


def _load_results(refs: dict[str, str]) -> list[DomainAnalysisResult]:
    """从轻量 State 中的文件引用恢复领域结果。"""

    return [
        DomainAnalysisResult.model_validate(ArtifactStore.read_json(ref))
        for ref in refs.values()
    ]


def review_analysis_coverage(state: dict) -> dict:
    """执行确定性最低覆盖检查，并覆盖写入最新检查快照。"""

    store = artifact_store_for_task(str(state["task_id"]))
    plan = AnalysisPlan.model_validate(store.read_json(state["plan_ref"]))
    results = _load_results(state.get("domain_result_refs") or {})
    gaps = review_coverage(plan.tasks, results)
    ref = store.write_json(
        "coverage.json",
        {
            "repair_count": int(state.get("repair_count") or 0),
            "gaps": [gap.model_dump(mode="json") for gap in gaps],
        },
    )
    return {
        "coverage_ref": ref,
        "coverage_gaps": [gap.model_dump(mode="json") for gap in gaps],
        "audit": [
            {
                "step": "review_coverage",
                "gap_count": len(gaps),
                "repair_count": int(state.get("repair_count") or 0),
            }
        ],
    }


def route_after_coverage(state: dict) -> str:
    """有缺口时最多修复一次，之后无论结果如何都进入报告。"""

    if state.get("coverage_gaps") and int(state.get("repair_count") or 0) < 1:
        return "repair"
    return "render"


def _repair_task(task: DomainTask, gaps: list[CoverageGap]) -> DomainTask:
    """把覆盖缺口转成聚焦目标，复用原领域研究子图定向补充。"""

    missing_types = []
    for gap in gaps:
        if gap.dimension != "scenario_type":
            continue
        try:
            scenario_type = ScenarioType(gap.missing_item)
        except ValueError:
            continue
        if scenario_type not in missing_types:
            missing_types.append(scenario_type)
    objectives = [
        f"补充覆盖缺口：{gap.dimension}/{gap.missing_item}（{gap.reason}）"
        for gap in gaps
    ]
    return DomainTask(
        task_id=f"{task.task_id}_repair",
        name=f"{task.name} - 定向补充",
        domain=task.domain,
        channels=task.channels,
        objectives=objectives,
        required_scenario_types=missing_types or task.required_scenario_types,
        retrieval_queries=[*task.retrieval_queries, *objectives],
    )


def _merge_results(
    original: DomainAnalysisResult,
    repair: DomainAnalysisResult,
) -> DomainAnalysisResult:
    """按场景语义键和证据 chunk_id 幂等合并修复结果。"""

    scenarios = {
        (scenario.scenario_type.value, scenario.title.strip().lower()): scenario
        for scenario in original.scenarios
    }
    for scenario in repair.scenarios:
        scenarios[(scenario.scenario_type.value, scenario.title.strip().lower())] = scenario
    evidence = {hit.chunk_id: hit for hit in original.evidence}
    evidence.update({hit.chunk_id: hit for hit in repair.evidence})
    original.scenarios = list(scenarios.values())
    original.evidence = list(evidence.values())
    original.missing_topics = list(
        dict.fromkeys([*original.missing_topics, *repair.missing_topics])
    )
    original.warnings = list(dict.fromkeys([*original.warnings, *repair.warnings]))
    if original.scenarios and original.status == "failed":
        original.status = "partial"
    return original


def repair_coverage_gaps(state: dict) -> dict:
    """按原任务分组补充覆盖缺口，并原位更新领域产物。

    修复失败只写审计记录；路由计数仍会增加，防止相同缺口形成无限循环。
    """

    store = artifact_store_for_task(str(state["task_id"]))
    fact_data = store.read_json(state["requirement_ref"])
    plan = AnalysisPlan.model_validate(store.read_json(state["plan_ref"]))
    task_by_id = {task.task_id: task for task in plan.tasks}
    grouped = gaps_by_task(
        [CoverageGap.model_validate(item) for item in state.get("coverage_gaps") or []]
    )
    retriever = get_knowledge_retriever()
    research_graph = build_domain_research_graph()
    refs = dict(state.get("domain_result_refs") or {})
    audit: list[dict] = []

    for task_id, gaps in grouped.items():
        task = task_by_id.get(task_id)
        ref = refs.get(task_id)
        if task is None or ref is None:
            continue
        repair_task = _repair_task(task, gaps)
        try:
            output = research_graph.invoke(
                {
                    "requirement_fact": fact_data,
                    "domain_task": repair_task.model_dump(mode="json"),
                    "allowed_channels": retriever.expand_allowed_channels(
                        repair_task.channels
                    ),
                }
            )
            repair_result = DomainAnalysisResult.model_validate(output["result"])
            original = DomainAnalysisResult.model_validate(store.read_json(ref))
            merged = _merge_results(original, repair_result)
            store.write_json(
                f"domains/{task.task_id}.json",
                merged.model_dump(mode="json"),
            )
            audit.extend(output.get("audit") or [])
        except Exception as exc:
            audit.append(
                {
                    "step": "repair_domain_failed",
                    "task_id": task_id,
                    "error": str(exc),
                }
            )

    return {
        "domain_result_refs": refs,
        "repair_count": int(state.get("repair_count") or 0) + 1,
        "audit": [
            *audit,
            {
                "step": "repair_coverage_gaps",
                "task_count": len(grouped),
            },
        ],
    }


def render_analysis_report(state: dict) -> dict:
    """聚合最终状态、确定性渲染报告并写入运行清单。"""

    store = artifact_store_for_task(str(state["task_id"]))
    fact = RequirementFact.model_validate(store.read_json(state["requirement_ref"]))
    results = _load_results(state.get("domain_result_refs") or {})
    gaps = [
        CoverageGap.model_validate(item)
        for item in state.get("coverage_gaps") or []
    ]
    markdown = render_markdown(fact, results, gaps)
    report_ref = store.write_markdown("test_analysis.md", markdown)

    failed_domains = [
        result.domain for result in results if result.status == "failed"
    ]
    warnings = list(
        dict.fromkeys(
            [
                *[warning for result in results for warning in result.warnings],
                *[
                    f"{result.domain}: 缺少 {topic}"
                    for result in results
                    for topic in result.missing_topics
                ],
            ]
        )
    )
    scenario_count = sum(len(result.scenarios) for result in results)
    # partial 表示已有可用场景但仍有明确风险，不能用 completed 掩盖资料缺口。
    if not results or scenario_count == 0:
        status = "failed"
    elif gaps or failed_domains or warnings:
        status = "partial"
    else:
        status = "completed"

    output = AnalysisOutput(
        status=status,
        summary=(
            f"生成 {scenario_count} 条测试场景，"
            f"覆盖缺口 {len(gaps)} 项，失败领域 {len(failed_domains)} 个"
        ),
        report_ref=report_ref,
        warnings=warnings,
        failed_domains=failed_domains,
    )
    store.write_json(
        "manifest.json",
        {
            "task_id": state["task_id"],
            "requirement_ref": state["requirement_ref"],
            "plan_ref": state["plan_ref"],
            "domain_result_refs": state.get("domain_result_refs") or {},
            "coverage_ref": state.get("coverage_ref"),
            "output": output.model_dump(mode="json"),
        },
    )
    return {
        "analysis_path": report_ref,
        "summary": {
            "status": output.status,
            "message": output.summary,
            "warnings": output.warnings,
            "failed_domains": output.failed_domains,
            "scenario_count": scenario_count,
            "coverage_gap_count": len(gaps),
        },
        "audit": [
            {
                "step": "render_analysis_report",
                "status": output.status,
                "analysis_path": report_ref,
                "scenario_count": scenario_count,
            }
        ],
    }
