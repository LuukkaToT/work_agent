"""
主图组装：只负责「有哪些节点、边怎么连」，业务逻辑在 nodes/ 与子图里。

当前完整路径：
  START → intake → router
                 ├─ analysis → test_analysis ────────────────┐
                 ├─ execute → [子图 exec_flow] ──────────────┤
                 ├─ query → query_run ───────────────────────┤
                 └─ chat → quick_answer ─────────────────────┤
                                                             │
                                          respond → END ◄────┘

exec_flow 子图内部：
  exec_params → ask_missing → confirm_exec
                    ├─ proceed → exec_run → exec_poll ⟲ → collect → write_report
                    └─ cancel  → write_report
"""

from __future__ import annotations

import uuid
from typing import Sequence

from langgraph.graph import END, START, StateGraph
from langgraph.types import Checkpointer

from work_agent.graph.nodes.analysis import test_analysis
from work_agent.graph.nodes.chat import quick_answer
from work_agent.graph.nodes.query_run import query_run
from work_agent.graph.nodes.respond import respond
from work_agent.graph.nodes.router import route_by_intent, router
from work_agent.graph.state import RESET_AUDIT, TestFlowState
from work_agent.graph.subgraphs.exec_flow import build_exec_flow


def _message_text(content: object) -> str:
    """把消息 content（str 或分段 list）归一成纯文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "".join(parts).strip()
    return str(content or "").strip()


def intake(state: TestFlowState) -> dict:
    """
    每轮入口：从 messages 提取本轮用户输入，并归零全部任务级字段。

    调用方只需 invoke({"messages": [HumanMessage(...)]})。
    会话级 messages 由 add_messages 累积，intake 绝不碰它。
    """
    messages = state.get("messages") or []
    if not messages:
        raise ValueError("intake 需要至少一条用户消息，请 invoke 时传入 messages")

    user_input = _message_text(messages[-1].content)
    if not user_input:
        raise ValueError("本轮用户消息为空")

    # 每轮新任务：不要复用上一轮 task_id（否则报告会盖到旧目录）
    task_id = str(uuid.uuid4())[:8]

    return {
        "task_id": task_id,
        "user_input": user_input,
        # --- 以下全部任务级字段显式归零，防止跨轮串数据 ---
        "intent": "",
        "requirement": "",
        "analysis_path": "",
        "exec_params": {},
        "run_id": "",
        "run_status": "",
        "results": [],
        "logs": "",
        "report_path": "",
        "summary": {},
        "reply": "",
        "audit": [
            {RESET_AUDIT: True},
            {
                "step": "intake",
                "task_id": task_id,
                "user_input": user_input,
            },
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
    graph.add_node("exec_flow", build_exec_flow())
    graph.add_node("query_run", query_run)
    graph.add_node("quick_answer", quick_answer)
    graph.add_node("respond", respond)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "router")
    graph.add_conditional_edges(
        "router",
        route_by_intent,
        {
            "analysis": "test_analysis",
            "execute": "exec_flow",
            "query": "query_run",
            "chat": "quick_answer",
        },
    )

    graph.add_edge("test_analysis", "respond")
    graph.add_edge("exec_flow", "respond")
    graph.add_edge("query_run", "respond")
    graph.add_edge("quick_answer", "respond")
    graph.add_edge("respond", END)

    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before) if interrupt_before else None,
    )
