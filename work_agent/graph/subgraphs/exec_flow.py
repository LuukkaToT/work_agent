"""
执行流水线子图。

节点实现在 nodes/exec_flow.py、hitl.py；
这里只定义 Input / Output / 私有三层 state，并接线编译。

audit 只出不进：不在 Input 里，子图从空列表记起，
Output 交回本子图新增记录，避免父图 append 时重复计入。
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from work_agent.graph.nodes.exec_flow import create_pipelines, exec_params
from work_agent.graph.nodes.hitl import (
    ask_missing,
    confirm_exec,
    route_after_confirm,
)
from work_agent.graph.state import append_audit


class ExecFlowInput(TypedDict):
    """父图 → 子图：只读上下文。"""

    user_input: str
    task_id: str
    intent: str
    messages: Annotated[list[AnyMessage], add_messages]
    dialogue_summary: str


class ExecFlowOutput(TypedDict):
    """子图 → 父图：respond / CLI 的消费面。"""

    exec_params: dict
    pipelines: list[dict]
    summary: dict
    audit: Annotated[list[dict], append_audit]


class ExecFlowState(ExecFlowInput, ExecFlowOutput):
    """子图内部全量 = 输入 + 产出 + 私有字段。"""

    exec_decision: str  # proceed | cancel


def build_exec_flow():
    """编译执行子图（无独立 checkpointer；由父图编译时注入）。"""
    graph = StateGraph(
        ExecFlowState,
        input_schema=ExecFlowInput,
        output_schema=ExecFlowOutput,
    )

    graph.add_node("exec_params", exec_params)
    graph.add_node("ask_missing", ask_missing)
    graph.add_node("confirm_exec", confirm_exec)
    graph.add_node("create_pipelines", create_pipelines)

    graph.add_edge(START, "exec_params")
    graph.add_edge("exec_params", "ask_missing")
    graph.add_edge("ask_missing", "confirm_exec")
    graph.add_conditional_edges(
        "confirm_exec",
        route_after_confirm,
        {
            "proceed": "create_pipelines",
            "cancel": END,
        },
    )
    graph.add_edge("create_pipelines", END)

    return graph.compile()
