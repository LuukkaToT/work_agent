"""带 LangGraph ToolNode 的单领域资料研究子图。"""

from __future__ import annotations

from functools import lru_cache
import json
import operator
import re
from typing import Annotated, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from work_agent.analysis.corpus import get_knowledge_retriever
from work_agent.analysis.llm import invoke_structured
from work_agent.analysis.prompts import load_prompt
from work_agent.analysis.schemas import (
    DomainAnalysisResult,
    DomainTask,
    EvidenceAssessment,
    EvidenceHit,
    RequirementFact,
)
from work_agent.analysis.tools import RESEARCH_TOOLS
from work_agent.core.config import get_settings
from work_agent.core.llm import get_chat_model
from work_agent.graph.state import append_audit


class DomainResearchInput(TypedDict):
    requirement_fact: dict
    domain_task: dict
    allowed_channels: list[str]


class DomainResearchOutput(TypedDict):
    result: dict
    audit: Annotated[list[dict], append_audit]


class DomainResearchState(DomainResearchInput, DomainResearchOutput):
    research_messages: Annotated[list[AnyMessage], add_messages]
    tool_call_count: int
    retrieval_round: int
    evidence: list[dict]
    assessment: dict
    warnings: Annotated[list[str], operator.add]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _error_text(exc: Exception) -> str:
    return re.sub(r"\s+", " ", str(exc)).strip()[:300]


def initialize_research(state: DomainResearchState) -> dict:
    task = DomainTask.model_validate(state["domain_task"])
    return {
        "research_messages": [],
        "tool_call_count": 0,
        "retrieval_round": 0,
        "evidence": [],
        "assessment": {},
        "result": {},
        "audit": [{"step": "research_initialize", "task_id": task.task_id}],
        "warnings": [],
    }


def _agent_input_messages(state: DomainResearchState) -> list[AnyMessage]:
    """为每次 Tool 决策构造一个独立回合。

    Gemini 3 的 OpenAI 兼容端点要求原样回传 thought_signature，而当前
    ChatOpenAI 消息转换不会保留 provider 扩展字段。这里不回放旧的函数调用，
    只把去重后的检索结果作为新 HumanMessage 文本输入；ToolNode 仍负责真实
    Tool 执行。该方式也避免研究消息随轮次无限增长。
    """
    fact = RequirementFact.model_validate(state["requirement_fact"])
    task = DomainTask.model_validate(state["domain_task"])
    evidence = _tool_evidence(state.get("research_messages") or [])[:16]
    assessment = state.get("assessment") or {}
    instruction = (
        "尚无检索证据：必须先查基础测试点库；任务包含信道时还必须查对应信道库。"
        if not evidence
        else "根据已有证据判断是否还需调用工具；资料足够时直接说明研究完成。"
    )
    return [
        SystemMessage(content=load_prompt("research")),
        HumanMessage(
            content=(
                f"{instruction}\n\n"
                f"【需求事实】\n{_json(fact.model_dump(mode='json'))}\n\n"
                f"【领域任务】\n{_json(task.model_dump(mode='json'))}\n\n"
                f"【允许检索的信道】\n{_json(state.get('allowed_channels') or [])}\n\n"
                f"【已有证据】\n"
                f"{_json([item.model_dump(mode='json') for item in evidence])}\n\n"
                f"【上一轮充分性判断】\n{_json(assessment)}"
            )
        ),
    ]


def research_agent(state: DomainResearchState) -> dict:
    max_calls = get_settings().test_analysis_max_tool_calls
    remaining = max(0, max_calls - int(state.get("tool_call_count") or 0))
    try:
        response = (
            get_chat_model(temperature=0.1)
            .bind_tools(RESEARCH_TOOLS)
            .invoke(_agent_input_messages(state))
        )
    except Exception as exc:
        error = _error_text(exc)
        return {
            "research_messages": [
                AIMessage(content=f"检索模型调用失败，进入确定性兜底检索：{error}")
            ],
            "warnings": [f"检索模型调用失败: {error}"],
        }

    calls = list(getattr(response, "tool_calls", []) or [])
    if len(calls) > remaining:
        response = AIMessage(
            content=response.content or "",
            tool_calls=calls[:remaining],
        )
        calls = calls[:remaining]
    return {
        "research_messages": [response],
        "tool_call_count": int(state.get("tool_call_count") or 0) + len(calls),
        "audit": [
            {
                "step": "research_agent",
                "tool_calls": [call.get("name") for call in calls],
            }
        ],
    }


def route_after_research_agent(state: DomainResearchState) -> str:
    decision = tools_condition(state, messages_key="research_messages")
    if (
        decision == "tools"
        and int(state.get("tool_call_count") or 0)
        <= get_settings().test_analysis_max_tool_calls
    ):
        return "tools"
    return "assess"


def _tool_evidence(messages: list[AnyMessage]) -> list[EvidenceHit]:
    hits: dict[str, EvidenceHit] = {}
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            payload = json.loads(str(message.content))
        except (TypeError, json.JSONDecodeError):
            continue
        for raw in payload.get("hits") or []:
            try:
                hit = EvidenceHit.model_validate(raw)
            except Exception:
                continue
            hits[hit.chunk_id] = hit
    return list(hits.values())


def _fallback_evidence(state: DomainResearchState) -> list[EvidenceHit]:
    task = DomainTask.model_validate(state["domain_task"])
    retriever = get_knowledge_retriever()
    queries = task.retrieval_queries or task.objectives or [task.name]
    evidence = retriever.search_basic(
        queries=queries,
        dimensions=[item.value for item in task.required_scenario_types],
        top_k=3,
    )
    for channel in task.channels[:2]:
        evidence.extend(
            retriever.search_channel(
                channel=channel,
                queries=queries,
                top_k=3,
            )
        )
    deduped = {hit.chunk_id: hit for hit in evidence}
    return list(deduped.values())


def assess_evidence(state: DomainResearchState) -> dict:
    fact = RequirementFact.model_validate(state["requirement_fact"])
    task = DomainTask.model_validate(state["domain_task"])
    evidence = _tool_evidence(state.get("research_messages") or [])
    if not evidence:
        evidence = _fallback_evidence(state)

    # 父图永不接收 research_messages；这里只把去重后的有限证据交给评估模型。
    evidence = evidence[:16]
    try:
        assessment = invoke_structured(
            EvidenceAssessment,
            [
                SystemMessage(content=load_prompt("assess_evidence")),
                HumanMessage(
                    content=(
                        f"【需求】\n{_json(fact.model_dump(mode='json'))}\n\n"
                        f"【任务】\n{_json(task.model_dump(mode='json'))}\n\n"
                        f"【证据】\n{_json([item.model_dump(mode='json') for item in evidence])}"
                    )
                ),
            ],
        )
    except Exception as exc:
        error = _error_text(exc)
        assessment = EvidenceAssessment(
            status="sufficient" if evidence else "corpus_gap",
            covered_topics=task.objectives if evidence else [],
            missing_topics=[] if evidence else task.objectives,
            reason=f"充分性模型不可用，使用代码兜底判断: {error}",
        )

    return {
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "assessment": assessment.model_dump(mode="json"),
        "retrieval_round": int(state.get("retrieval_round") or 0) + 1,
        "audit": [
            {
                "step": "assess_evidence",
                "status": assessment.status,
                "evidence_count": len(evidence),
                "missing_topics": assessment.missing_topics,
            }
        ],
    }


def route_after_assessment(state: DomainResearchState) -> str:
    assessment = EvidenceAssessment.model_validate(state["assessment"])
    can_retry = (
        assessment.status in {"retry_search", "expand_channels"}
        and bool(assessment.followup_queries)
        and int(state.get("retrieval_round") or 0)
        < get_settings().test_analysis_max_retrieval_rounds
        and int(state.get("tool_call_count") or 0)
        < get_settings().test_analysis_max_tool_calls
    )
    return "retry" if can_retry else "generate"


def request_more_research(state: DomainResearchState) -> dict:
    assessment = EvidenceAssessment.model_validate(state["assessment"])
    return {
        "research_messages": [
            HumanMessage(
                content=(
                    "当前证据仍有缺口，请使用工具做最后一轮定向检索。\n"
                    f"缺失主题：{_json(assessment.missing_topics)}\n"
                    f"建议查询：{_json(assessment.followup_queries)}\n"
                    f"建议扩展信道：{_json(assessment.additional_channels)}"
                )
            )
        ],
        "audit": [{"step": "request_more_research"}],
    }


def generate_domain_result(state: DomainResearchState) -> dict:
    fact = RequirementFact.model_validate(state["requirement_fact"])
    task = DomainTask.model_validate(state["domain_task"])
    assessment = EvidenceAssessment.model_validate(state["assessment"])
    evidence = [EvidenceHit.model_validate(item) for item in state.get("evidence") or []]
    valid_refs = {item.chunk_id for item in evidence}

    try:
        result = invoke_structured(
            DomainAnalysisResult,
            [
                SystemMessage(content=load_prompt("generate_scenarios")),
                HumanMessage(
                    content=(
                        f"【需求事实】\n{_json(fact.model_dump(mode='json'))}\n\n"
                        f"【领域任务】\n{_json(task.model_dump(mode='json'))}\n\n"
                        f"【资料充分性】\n{_json(assessment.model_dump(mode='json'))}\n\n"
                        f"【可引用证据】\n{_json([item.model_dump(mode='json') for item in evidence])}"
                    )
                ),
            ],
            temperature=0.2,
        )
    except Exception as exc:
        error = _error_text(exc)
        result = DomainAnalysisResult(
            task_id=task.task_id,
            domain=task.domain,
            channels=task.channels,
            status="failed",
            summary="领域场景生成失败",
            evidence=evidence,
            missing_topics=assessment.missing_topics,
            warnings=[error],
        )

    result.task_id = task.task_id
    result.domain = task.domain
    result.channels = list(dict.fromkeys(task.channels))
    result.evidence = evidence
    result.missing_topics = list(dict.fromkeys(assessment.missing_topics))
    result.warnings.extend(state.get("warnings") or [])
    for scenario in result.scenarios:
        scenario.domain = task.domain
        scenario.channels = list(
            dict.fromkeys(scenario.channels or task.channels)
        )
        scenario.evidence_refs = [
            ref for ref in scenario.evidence_refs if ref in valid_refs
        ]
    if assessment.status == "corpus_gap" and result.status == "completed":
        result.status = "partial"
    if not result.scenarios:
        result.status = "failed"

    return {
        "result": result.model_dump(mode="json"),
        "audit": [
            {
                "step": "generate_domain_result",
                "task_id": task.task_id,
                "status": result.status,
                "scenario_count": len(result.scenarios),
            }
        ],
    }


@lru_cache(maxsize=1)
def build_domain_research_graph():
    graph = StateGraph(
        DomainResearchState,
        input_schema=DomainResearchInput,
        output_schema=DomainResearchOutput,
    )
    graph.add_node("initialize", initialize_research)
    graph.add_node("research_agent", research_agent)
    graph.add_node(
        "research_tools",
        ToolNode(
            RESEARCH_TOOLS,
            messages_key="research_messages",
            handle_tool_errors=True,
        ),
    )
    graph.add_node("assess_evidence", assess_evidence)
    graph.add_node("request_more_research", request_more_research)
    graph.add_node("generate_domain_result", generate_domain_result)

    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "research_agent")
    graph.add_conditional_edges(
        "research_agent",
        route_after_research_agent,
        {
            "tools": "research_tools",
            "assess": "assess_evidence",
        },
    )
    graph.add_edge("research_tools", "research_agent")
    graph.add_conditional_edges(
        "assess_evidence",
        route_after_assessment,
        {
            "retry": "request_more_research",
            "generate": "generate_domain_result",
        },
    )
    graph.add_edge("request_more_research", "research_agent")
    graph.add_edge("generate_domain_result", END)
    return graph.compile()
