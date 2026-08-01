"""
主图组装：只负责「有哪些节点、边怎么连」，业务逻辑在 nodes/ 里。

当前完整路径：
  START → intake → router
                 ├─ analysis → test_analysis → END
                 ├─ execute → exec_params → ask_missing → confirm_exec
                 │                ├─ proceed → exec_run → exec_poll ⟲ → collect → report
                 │                └─ cancel  → write_report
                 ├─ query → query_run → END
                 └─ chat → quick_answer → END
"""

from __future__ import annotations

import uuid
from typing import Sequence

from langgraph.graph import END, START, StateGraph
from langgraph.types import Checkpointer

from work_agent.graph.nodes.analysis import test_analysis
from work_agent.graph.nodes.chat import quick_answer
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
from work_agent.graph.nodes.query_run import query_run
from work_agent.graph.nodes.report import collect_results, write_report
from work_agent.graph.nodes.router import route_by_intent, router
from work_agent.graph.state import TestFlowState


def intake(state: TestFlowState) -> dict:
    """入口节点：生成 task_id，写下第一条 audit。"""
    task_id = state.get("task_id") or str(uuid.uuid4())[:8]
    return {
        "task_id": task_id,
        "audit": [
            {
                "step": "intake",
                "task_id": task_id,
                "user_input": state["user_input"],
            }
        ],
    }


def build_graph(
    *,
    checkpointer: Checkpointer | None = None,
    interrupt_before: Sequence[str] | None = None,
):
    graph = StateGraph(TestFlowState)

    graph.add_node("intake", intake)
    graph.add_node("router", router)
    graph.add_node("test_analysis", test_analysis)
    graph.add_node("exec_params", exec_params)
    graph.add_node("ask_missing", ask_missing)
    graph.add_node("confirm_exec", confirm_exec)
    graph.add_node("exec_run", exec_run)
    graph.add_node("exec_poll", exec_poll)
    graph.add_node("collect_results", collect_results)
    graph.add_node("write_report", write_report)
    graph.add_node("query_run", query_run)
    graph.add_node("quick_answer", quick_answer)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "router")
    graph.add_conditional_edges(
        "router",
        route_by_intent,
        {
            "analysis": "test_analysis",
            "execute": "exec_params",
            "query": "query_run",
            "chat": "quick_answer",
        },
    )

    graph.add_edge("test_analysis", END)
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
    graph.add_edge("query_run", END)
    graph.add_edge("quick_answer", END)

    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before) if interrupt_before else None,
    )
