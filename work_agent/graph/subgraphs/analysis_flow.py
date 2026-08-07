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
    """父图允许传入测试分析子图的最小输入。"""

    task_id: str
    user_input: str


class TestAnalysisOutput(TypedDict):
    """子图对父图公开的结果；大对象仅通过文件引用返回。"""

    requirement: str
    analysis_path: str
    summary: dict
    audit: Annotated[list[dict], append_audit]


class TestAnalysisState(TestAnalysisInput, TestAnalysisOutput):
    """测试分析私有状态。

    requirement/plan/domain result 的完整内容写入 ArtifactStore，State 只保存
    引用和小型摘要，避免 Checkpoint 随资料和场景数量持续膨胀。
    """

    requirement_ref: str
    plan_ref: str
    domain_result_refs: dict[str, str]
    domain_summaries: dict[str, dict]
    coverage_ref: str
    coverage_gaps: list[dict]
    repair_count: int


def build_test_analysis_graph():
    """编译完整测试分析子图。

    覆盖修复通过条件边最多回流一次；终止约束由 ``repair_count`` 在路由节点
    中执行，而不是交给模型自行判断。
    """

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
