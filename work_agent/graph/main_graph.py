import uuid

from langgraph.graph import END, START, StateGraph

from work_agent.graph.nodes.branches import (
    do_analysis,
    do_chat,
    do_execute,
    do_query,
)
from work_agent.graph.nodes.router import route_by_intent, router
from work_agent.graph.state import TestFlowState


def intake(state: TestFlowState) -> dict:
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


def build_graph():
    graph = StateGraph(TestFlowState)

    graph.add_node("intake", intake)
    graph.add_node("router", router)
    graph.add_node("analysis", do_analysis)
    graph.add_node("execute", do_execute)
    graph.add_node("query", do_query)
    graph.add_node("chat", do_chat)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "router")
    graph.add_conditional_edges(
        "router",
        route_by_intent,
        { # 这里是路由表
            "analysis": "analysis",
            "execute": "execute",
            "query": "query",
            "chat": "chat",
        },
    )
    graph.add_edge("analysis", END)
    graph.add_edge("execute", END)
    graph.add_edge("query", END)
    graph.add_edge("chat", END)

    return graph.compile()