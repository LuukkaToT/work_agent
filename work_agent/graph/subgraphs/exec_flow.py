"""
执行流水线子图。

节点实现仍在 nodes/exec_flow.py、hitl.py、report.py；
这里只定义 Input / Output / 私有三层 state，并接线编译。

audit 只出不进：不在 Input 里，子图从空列表记起，
Output 交回本子图新增记录，避免父图 append 时重复计入。
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from work_agent.graph.nodes.exec_flow import (
    exec_params,
    exec_poll,
    exec_run,
    route_after_poll,
)
from work_agent.graph.nodes.hitl import (
    ask_missing,
    confirm_exec,
    route_after_confirm,
)
from work_agent.graph.nodes.report import collect_results, write_report
from work_agent.graph.state import append_audit


class ExecFlowInput(TypedDict):
    """父图 → 子图：只读上下文。"""

    user_input: str
    task_id: str
    intent: str
    messages: Annotated[list[AnyMessage], add_messages]


class ExecFlowOutput(TypedDict):
    """子图 → 父图：respond / CLI 的消费面。"""

    exec_params: dict
    run_id: str
    run_status: str
    results: list[dict]
    logs: str
    report_path: str
    summary: dict
    audit: Annotated[list[dict], append_audit]


class ExecFlowState(ExecFlowInput, ExecFlowOutput):
    """子图内部全量 = 输入 + 产出 + 私有字段。"""

    cases: list[dict]  # 确认提示用的用例元信息
    exec_decision: str  # proceed | cancel
    poll_count: int  # 轮询刹车计数


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
    graph.add_node("exec_run", exec_run)
    graph.add_node("exec_poll", exec_poll)
    graph.add_node("collect_results", collect_results)
    graph.add_node("write_report", write_report)

    graph.add_edge(START, "exec_params")
    graph.add_edge("exec_params", "ask_missing")
    graph.add_edge("ask_missing", "confirm_exec")
    graph.add_conditional_edges(
        "confirm_exec",
        route_after_confirm,
        {
            "proceed": "exec_run",
            "cancel": "write_report",
        },
    )
    graph.add_edge("exec_run", "exec_poll")
    graph.add_conditional_edges(
        "exec_poll",
        route_after_poll,
        {
            "continue": "exec_poll",
            "done": "collect_results",
        },
    )
    graph.add_edge("collect_results", "write_report")
    graph.add_edge("write_report", END)

    return graph.compile()
