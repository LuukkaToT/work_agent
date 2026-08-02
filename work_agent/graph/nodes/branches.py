"""
占位分支节点（历史对照用，主图已不再注册）。

真正实现见 analysis / exec_flow / query_run / chat。
"""

from work_agent.graph.state import TestFlowState


def _branch(name: str, state: TestFlowState) -> dict:
    return {
        "summary": {"status": "ok", "message": f"stub:{name}"},
        "audit": [{"step": name, "task_id": state.get("task_id")}],
    }


def do_analysis(state: TestFlowState) -> dict:
    return _branch("analysis", state)


def do_execute(state: TestFlowState) -> dict:
    return _branch("execute", state)


def do_query(state: TestFlowState) -> dict:
    return _branch("query", state)


def do_chat(state: TestFlowState) -> dict:
    return _branch("chat", state)
