"""
主图组装。

  START → intake → router
                 ├─ analysis → test_analysis ──┐
                 ├─ execute → exec_flow ───────┤
                 ├─ start → prepare_start → start_pipelines ─┤
                 ├─ query → query_run ─────────┤
                 └─ chat → quick_answer ───────┤
                                               │
                            respond → memory → END
"""

from __future__ import annotations

import uuid
from typing import Sequence

from langgraph.graph import END, START, StateGraph
from langgraph.types import Checkpointer

from work_agent.graph.nodes.chat import quick_answer
from work_agent.graph.nodes.exec_flow import start_pipelines
from work_agent.graph.nodes.memory import memory
from work_agent.graph.nodes.prepare_start import prepare_start
from work_agent.graph.nodes.query_run import query_run
from work_agent.graph.nodes.respond import respond
from work_agent.graph.nodes.router import route_by_intent, router
from work_agent.graph.state import RESET_AUDIT, TestFlowState
from work_agent.graph.subgraphs.exec_flow import build_exec_flow
from work_agent.graph.subgraphs.analysis_flow import build_test_analysis_graph


def _message_text(content: object) -> str:
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
    messages = state.get("messages") or []
    if not messages:
        raise ValueError("intake 需要至少一条用户消息，请 invoke 时传入 messages")

    user_input = _message_text(messages[-1].content)
    if not user_input:
        raise ValueError("本轮用户消息为空")

    task_id = str(uuid.uuid4())[:8]

    return {
        "task_id": task_id,
        "user_input": user_input,
        "intent": "",
        "requirement": "",
        "analysis_path": "",
        "exec_params": {},
        "pipelines": [],
        "results": [],
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
    # 测试分析以编译后的子图挂载，父图只能看到其 Input/Output 契约。
    graph.add_node("test_analysis", build_test_analysis_graph())
    graph.add_node("exec_flow", build_exec_flow())
    graph.add_node("prepare_start", prepare_start)
    graph.add_node("start_pipelines", start_pipelines)
    graph.add_node("query_run", query_run)
    graph.add_node("quick_answer", quick_answer)
    graph.add_node("respond", respond)
    graph.add_node("memory", memory)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "router")
    graph.add_conditional_edges(
        "router",
        route_by_intent,
        {
            "analysis": "test_analysis",
            "execute": "exec_flow",
            "start": "prepare_start",
            "query": "query_run",
            "chat": "quick_answer",
        },
    )

    graph.add_edge("test_analysis", "respond")
    graph.add_edge("exec_flow", "respond")
    graph.add_conditional_edges(
        "prepare_start",
        lambda s: "start" if (s.get("pipelines") or []) else "skip",
        {"start": "start_pipelines", "skip": "respond"},
    )
    graph.add_edge("start_pipelines", "respond")
    graph.add_edge("query_run", "respond")
    graph.add_edge("quick_answer", "respond")
    graph.add_edge("respond", "memory")
    graph.add_edge("memory", END)

    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before) if interrupt_before else None,
    )
