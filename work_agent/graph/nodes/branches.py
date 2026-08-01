"""
占位分支节点：证明 router 条件边能走到对应分支。

- execute 已被 exec_flow 替换，do_execute 留着仅作对照，主图不再注册它
- analysis / query / chat 以后会换成真正的 Role / Flow
"""

from work_agent.graph.state import TestFlowState


def _branch(name: str, state: TestFlowState) -> dict:
    """统一桩实现：只写 summary + audit，方便 lesson 里检查走了哪条支路。"""
    return {
        "summary": {
            "status": "ok",
            "branch": name,
            "task_id": state["task_id"],
            "intent": state["intent"],
            "user_input": state["user_input"],
        },
        "audit": [{"step": name, "task_id": state["task_id"]}],
    }


def do_analysis(state: TestFlowState) -> dict:
    return _branch("analysis", state)


def do_execute(state: TestFlowState) -> dict:
    return _branch("execute", state)


def do_query(state: TestFlowState) -> dict:
    return _branch("query", state)


def do_chat(state: TestFlowState) -> dict:
    return _branch("chat", state)