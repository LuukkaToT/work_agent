import uuid

from langgraph.graph import END, START, StateGraph

from work_agent.graph.state import TestFlowState


def intake(state: TestFlowState) -> dict:
    """读 requirement，生成 task_id，写一条 audit。"""
    task_id = str(uuid.uuid4())[:8]
    return {
        "task_id": task_id,
        "audit": [
            {
                "step": "intake",
                "task_id": task_id,
                "requirement": state["requirement"],
            }
        ],
    }


def summarize(state: TestFlowState) -> dict:
    """根据当前 state 写一个简单 summary，再追加一条 audit。"""
    return {
        "summary": {
            "status": "ok",
            "task_id": state["task_id"],
            "requirement": state["requirement"],
        },
        "audit": [
            {
                "step": "summarize",
                "task_id": state["task_id"],
            }
        ],
    }

def build_graph():
    graph = StateGraph(TestFlowState)
    graph.add_node("intake", intake)
    graph.add_node("summarize", summarize)
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "summarize")
    graph.add_edge("summarize", END)
    return graph.compile()