"""
主图组装：只负责「有哪些节点、边怎么连」，业务逻辑在 nodes/ 里。

当前执行分支完整路径：
  START → intake → router
                 ├─ analysis → END          （桩）
                 ├─ execute → exec_params → ask_missing → confirm_exec
                 │                ├─ proceed → exec_run → exec_poll ⟲ → collect → report
                 │                └─ cancel  → write_report
                 ├─ query → END             （桩）
                 └─ chat → END              （桩）
"""

from __future__ import annotations

import uuid
from typing import Sequence

from langgraph.graph import END, START, StateGraph
from langgraph.types import Checkpointer

from work_agent.graph.nodes.branches import do_analysis, do_chat, do_query
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
    """
    编译主图。

    checkpointer:
      传入后必须在 invoke 时带 config={"configurable": {"thread_id": "..."}}
      HITL（节点内 interrupt）也依赖它保存暂停点。

    interrupt_before:
      在进入这些节点「之前」固定暂停（M10 演示用）。
      M11 主要用节点内 interrupt()，一般不必再设这个。
    """
    graph = StateGraph(TestFlowState)

    graph.add_node("intake", intake)
    graph.add_node("router", router)
    graph.add_node("analysis", do_analysis)
    graph.add_node("exec_params", exec_params)
    graph.add_node("ask_missing", ask_missing)
    graph.add_node("confirm_exec", confirm_exec)
    graph.add_node("exec_run", exec_run)
    graph.add_node("exec_poll", exec_poll)
    graph.add_node("collect_results", collect_results)
    graph.add_node("write_report", write_report)
    graph.add_node("query", do_query)
    graph.add_node("chat", do_chat)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "router")
    graph.add_conditional_edges(
        "router",
        route_by_intent,
        {
            "analysis": "analysis",
            "execute": "exec_params",
            "query": "query",
            "chat": "chat",
        },
    )

    graph.add_edge("analysis", END)

    # 抽参 → 补缺(HITL) → 确认(HITL) → 通过才提交
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
    graph.add_edge("query", END)
    graph.add_edge("chat", END)

    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before) if interrupt_before else None,
    )
