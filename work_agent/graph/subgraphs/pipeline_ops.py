"""
已有流水线操作子图：resolve → start | query | diagnose。

与 exec_flow（新建）对称。diagnose 走受限 ReAct error_analysis。
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from work_agent.graph.nodes.error_analysis import error_analysis
from work_agent.graph.nodes.exec_flow import start_pipelines
from work_agent.graph.nodes.pipeline_ops import (
    init_ops_kind,
    query_pipelines,
    resolve_pipelines,
    route_after_resolve,
)
from work_agent.graph.state import append_audit


class PipelineOpsInput(TypedDict):
    user_input: str
    task_id: str
    intent: str
    messages: Annotated[list[AnyMessage], add_messages]
    dialogue_summary: str


class PipelineOpsOutput(TypedDict):
    pipelines: list[dict]
    results: list[dict]
    summary: dict
    reply: str
    analysis_path: str
    audit: Annotated[list[dict], append_audit]


class PipelineOpsState(PipelineOpsInput, PipelineOpsOutput):
    ops_kind: str  # start | query | diagnose


def build_pipeline_ops():
    graph = StateGraph(
        PipelineOpsState,
        input_schema=PipelineOpsInput,
        output_schema=PipelineOpsOutput,
    )

    graph.add_node("init_ops_kind", init_ops_kind)
    graph.add_node("resolve_pipelines", resolve_pipelines)
    graph.add_node("start_pipelines", start_pipelines)
    graph.add_node("query_pipelines", query_pipelines)
    graph.add_node("error_analysis", error_analysis)

    graph.add_edge(START, "init_ops_kind")
    graph.add_edge("init_ops_kind", "resolve_pipelines")
    graph.add_conditional_edges(
        "resolve_pipelines",
        route_after_resolve,
        {
            "start": "start_pipelines",
            "query": "query_pipelines",
            "diagnose": "error_analysis",
            "skip": END,
        },
    )
    graph.add_edge("start_pipelines", END)
    graph.add_edge("query_pipelines", END)
    graph.add_edge("error_analysis", END)

    return graph.compile()
