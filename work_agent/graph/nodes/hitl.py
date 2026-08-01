"""
Human-in-the-loop 节点。

核心 API：
  reply = interrupt(提问载荷)
  # 图在这里暂停；下次 invoke(Command(resume=用户回答)) 时，
  # interrupt() 的「返回值」就是 resume 传进来的内容，节点从这行继续往下跑。

注意：resume 之后，节点函数会「从头再执行」到 interrupt 那一行，
但 interrupt 不会再停，而是直接返回 resume 值。
"""

from langgraph.types import interrupt

from work_agent.graph.state import TestFlowState


def ask_missing(state: TestFlowState) -> dict:
    """缺 case_names / topology 时 interrupt 问人；齐了就直接放行。"""
    params = dict(state.get("exec_params") or {})
    case_names = list(params.get("case_names") or [])
    topology = (params.get("topology") or "").strip()

    if not case_names:
        reply = interrupt(
            {
                "type": "ask_case_names",
                "message": "请输入要执行的用例名（逗号分隔），例如 case_downlink_001",
                "current": params,
            }
        )
        # 允许用户回字符串或 {"case_names": [...]}
        if isinstance(reply, str):
            case_names = [x.strip() for x in reply.split(",") if x.strip()]
        elif isinstance(reply, dict):
            case_names = list(reply.get("case_names") or [])
        params["case_names"] = case_names

    if not (params.get("topology") or "").strip():
        reply = interrupt(
            {
                "type": "ask_topology",
                "message": "请输入逻辑组网，例如 topo_a / topo_b（不要猜，必须你确认）",
                "current": params,
                "frequent": ["topo_a", "topo_b"],
            }
        )
        if isinstance(reply, str):
            topology = reply.strip()
        elif isinstance(reply, dict):
            topology = str(reply.get("topology") or "").strip()
        params["topology"] = topology

    return {
        "exec_params": params,
        "audit": [{"step": "ask_missing", "params": params}],
    }


def confirm_exec(state: TestFlowState) -> dict:
    """执行前最后确认。用户回 no 则取消，不调用 Executor.run。"""
    params = state.get("exec_params") or {}
    decision = interrupt(
        {
            "type": "confirm_exec",
            "message": "确认执行？输入 yes 继续，no 取消",
            "params": params,
            "cases": state.get("cases") or [],
        }
    )

    text = str(decision).strip().lower()
    ok = text in {"yes", "y", "是", "确认", "true", "1"}

    if not ok:
        return {
            "summary": {
                "status": "cancelled",
                "branch": "execute",
                "exec_params": params,
                "reason": "user_rejected",
            },
            "audit": [{"step": "confirm_exec", "decision": "no"}],
        }

    return {
        "audit": [{"step": "confirm_exec", "decision": "yes"}],
    }


def route_after_confirm(state: TestFlowState) -> str:
    """yes → 去 exec_run；no → 直接写报告结束。"""
    summary = state.get("summary") or {}
    if summary.get("status") == "cancelled":
        return "cancel"
    return "proceed"