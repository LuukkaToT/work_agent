from work_agent.graph.state import TestFlowState


def _branch(name: str, state: TestFlowState) -> dict:
    """这是「条件路由能跑通」的 stub；真正的测试分析 / 执行 / 查询 / 问答，以后会替换这些占位实现。"""
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