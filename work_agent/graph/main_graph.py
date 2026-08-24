"""
主图组装。

  START → intake → router
                 ├─ analysis → test_analysis ──┐
                 ├─ execute → exec_flow ───────┤
                 ├─ start|query|diagnose → pipeline_ops ─┤
                 └─ chat → quick_answer ───────┤
                                               │
                            respond → memory → END
"""

from __future__ import annotations

import uuid
from typing import Sequence

from langgraph.graph import END, START, StateGraph
from langgraph.types import Checkpointer

from work_agent.graph.nodes.analysis import test_analysis
from work_agent.graph.nodes.chat import quick_answer
from work_agent.graph.nodes.memory import memory
from work_agent.graph.nodes.respond import respond
from work_agent.graph.nodes.router import route_by_intent, router
from work_agent.graph.state import RESET_AUDIT, TestFlowState
from work_agent.graph.subgraphs.exec_flow import build_exec_flow
from work_agent.graph.subgraphs.pipeline_ops import build_pipeline_ops


def _message_text(content: object) -> str:
    """把消息 content（str / list 块）归一成纯文本。"""
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
    每轮入口：从最新消息取用户输入，归零任务级字段并重置 audit。

    参数:
        state: 当前图状态；至少需要非空 ``messages``。

    返回:
        写入 ``task_id`` / ``user_input``，清空任务级字段，
        并通过 audit 哨兵重置本轮审计轨迹。
    """
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
    """
    组装并 compile 主图（intake → router → 各分支 → respond → memory）。

    参数:
        checkpointer: 可选持久化；None 则无跨轮 checkpoint。
        interrupt_before: 在指定节点名前打断（调试用）；默认不打断。

    返回:
        已 compile 的 LangGraph 应用。
    """
    graph = StateGraph(TestFlowState)

    graph.add_node("intake", intake)
    graph.add_node("router", router)
    graph.add_node("test_analysis", test_analysis)
    graph.add_node("exec_flow", build_exec_flow())
    graph.add_node("pipeline_ops", build_pipeline_ops())
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
            "start": "pipeline_ops",
            "query": "pipeline_ops",
            "diagnose": "pipeline_ops",
            "chat": "quick_answer",
        },
    )

    graph.add_edge("test_analysis", "respond")
    graph.add_edge("exec_flow", "respond")
    graph.add_edge("pipeline_ops", "respond")
    graph.add_edge("quick_answer", "respond")
    graph.add_edge("respond", "memory")
    graph.add_edge("memory", END)

    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before) if interrupt_before else None,
    )
