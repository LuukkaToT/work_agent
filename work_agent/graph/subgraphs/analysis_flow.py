"""5G 基带测试分析子图。

主图只提供 task_id/user_input；检索消息、领域任务和证据均为子图私有状态。
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from work_agent.analysis.nodes import (
    analyze_domains,
    extract_requirement,
    initialize_analysis,
    plan_domains,
    render_analysis_report,
    repair_coverage_gaps,
    review_analysis_coverage,
    route_after_coverage,
)
from work_agent.graph.state import append_audit


class TestAnalysisInput(TypedDict):
    task_id: str
    user_input: str


class TestAnalysisOutput(TypedDict):
    requirement: str
    analysis_path: str
    summary: dict
    audit: Annotated[list[dict], append_audit]


class TestAnalysisState(TestAnalysisInput, TestAnalysisOutput):
    requirement_ref: str
    plan_ref: str
    domain_result_refs: dict[str, str]
    domain_summaries: dict[str, dict]
    coverage_ref: str
    coverage_gaps: list[dict]
    repair_count: int


def build_test_analysis_graph():
    graph = StateGraph(
        TestAnalysisState,
        input_schema=TestAnalysisInput,
        output_schema=TestAnalysisOutput,
    )
    graph.add_node("initialize_analysis", initialize_analysis)
    graph.add_node("extract_requirement", extract_requirement)
    graph.add_node("plan_domains", plan_domains)
    graph.add_node("analyze_domains", analyze_domains)
    graph.add_node("review_coverage", review_analysis_coverage)
    graph.add_node("repair_gaps", repair_coverage_gaps)
    graph.add_node("render_report", render_analysis_report)

    graph.add_edge(START, "initialize_analysis")
    graph.add_edge("initialize_analysis", "extract_requirement")
    graph.add_edge("extract_requirement", "plan_domains")
    graph.add_edge("plan_domains", "analyze_domains")
    graph.add_edge("analyze_domains", "review_coverage")
    graph.add_conditional_edges(
        "review_coverage",
        route_after_coverage,
        {
            "repair": "repair_gaps",
            "render": "render_report",
        },
    )
    graph.add_edge("repair_gaps", "review_coverage")
    graph.add_edge("render_report", END)
    return graph.compile()
