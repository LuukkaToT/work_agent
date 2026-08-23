"""set_mode：把 router 判定的个人偏好目标值（当前只有 debug_mode）落到 user_config。"""

from __future__ import annotations

from work_agent.core.user_config import set_debug_mode
from work_agent.graph.state import TestFlowState


def set_mode(state: TestFlowState) -> dict:
    """
    intent=set_mode 的落库节点。

    router 已经把目标值写进 ``state["debug_mode"]``（结构化输出里的
    ``debug_mode_target``，见 ``nodes/router.py``），这里只负责持久化到
    ``core/user_config.py`` 并产出 summary，不用再问模型一次。

    参数:
        state: 读 ``user_id``、``debug_mode``（router 判定的目标值）。

    返回:
        ``summary``（status/debug_mode）与 audit。
    """
    user_id = state.get("user_id") or ""
    target = bool(state.get("debug_mode"))
    set_debug_mode(user_id, target)
    return {
        "summary": {"status": "ok", "debug_mode": target},
        "audit": [{"step": "set_mode", "user_id": user_id, "debug_mode": target}],
    }
