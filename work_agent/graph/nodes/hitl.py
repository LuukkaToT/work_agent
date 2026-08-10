"""
Human-in-the-loop 节点。

核心 API：
  reply = interrupt(提问载荷)
  # 图在这里暂停；下次 invoke(Command(resume=用户回答)) 时，
  # interrupt() 的「返回值」就是 resume 传进来的内容，节点从这行继续往下跑。

注意：resume 之后，节点函数会「从头再执行」到 interrupt 那一行，
但 interrupt 不会再停，而是直接返回 resume 值。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from langgraph.types import interrupt

from work_agent.graph.nodes.exec_flow import ALLOWED_VERSIONS, _plan_dict


def _parse_case_names(reply: Any) -> list[str]:
    """把 HITL 回答解析成用例名列表。"""
    if isinstance(reply, str):
        parts = re.split(r"[,，\s]+", reply.strip())
        return [x for x in parts if x]
    if isinstance(reply, dict):
        return list(reply.get("case_names") or [])
    if isinstance(reply, list):
        return [str(x).strip() for x in reply if str(x).strip()]
    return []


def _parse_env(reply: Any) -> str:
    """把 HITL 回答解析成环境 IP / 组网字符串。"""
    if isinstance(reply, str):
        return reply.strip()
    if isinstance(reply, dict):
        return str(reply.get("env") or reply.get("topology") or "").strip()
    return str(reply or "").strip()


def _parse_version(reply: Any) -> str:
    """把 HITL 回答解析成版本字符串（转大写）。"""
    if isinstance(reply, str):
        return reply.strip().upper()
    if isinstance(reply, dict):
        return str(reply.get("version") or "").strip().upper()
    return str(reply or "").strip().upper()


def ask_missing(state: Mapping[str, Any]) -> dict:
    """
    按计划逐条补缺参（用例/版本/环境）；逻辑组网会提示改物理 IP。

    参数:
        state: 读 ``exec_params.plans``（可缺省为空计划）。

    返回:
        补全后的 ``exec_params`` 与 audit；过程中可能多次 interrupt。
    """
    params = dict(state.get("exec_params") or {})
    plans = [dict(p) for p in (params.get("plans") or [])]
    if not plans:
        plans = [_plan_dict(case_names=[], version="", env="")]

    for idx, plan in enumerate(plans):
        while True:
            missing = list(plan.get("missing") or [])
            if not missing:
                break

            if "case_names" in missing:
                reply = interrupt(
                    {
                        "type": "ask_case_names",
                        "message": (
                            f"第 {idx + 1}/{len(plans)} 条计划缺少用例名，"
                            "请输入用例名（逗号分隔）"
                        ),
                        "plan_index": idx,
                        "current": plan,
                    }
                )
                plan["case_names"] = _parse_case_names(reply)

            if "version" in missing:
                reply = interrupt(
                    {
                        "type": "ask_version",
                        "message": (
                            f"第 {idx + 1}/{len(plans)} 条计划缺少或版本无效，"
                            f"请输入 {', '.join(sorted(ALLOWED_VERSIONS))} 之一"
                        ),
                        "plan_index": idx,
                        "current": plan,
                        "allowed": sorted(ALLOWED_VERSIONS),
                    }
                )
                plan["version"] = _parse_version(reply)

            if "env" in missing:
                env_kind = plan.get("env_kind") or ""
                if env_kind == "logical":
                    msg = (
                        f"第 {idx + 1}/{len(plans)} 条计划给的是逻辑组网 "
                        f"`{plan.get('env')}`，现阶段只支持物理 IP。"
                        "请输入物理环境 IP，例如 7.223.50.60"
                        "（逻辑组网型号映射后续接入）"
                    )
                else:
                    msg = (
                        f"第 {idx + 1}/{len(plans)} 条计划缺少物理组网 IP，"
                        "请输入如 7.223.50.60"
                    )
                reply = interrupt(
                    {
                        "type": "ask_env",
                        "message": msg,
                        "plan_index": idx,
                        "current": plan,
                    }
                )
                plan["env"] = _parse_env(reply)

            # 重新计算 missing / env_kind
            rebuilt = _plan_dict(
                case_names=list(plan.get("case_names") or []),
                version=str(plan.get("version") or ""),
                env=str(plan.get("env") or ""),
            )
            plan.update(rebuilt)
            # 若用户仍给逻辑组网，继续循环问
            if plan.get("missing"):
                continue
            break

        plans[idx] = plan

    params["plans"] = plans
    return {
        "exec_params": params,
        "audit": [{"step": "ask_missing", "plan_count": len(plans), "params": params}],
    }


def confirm_exec(state: Mapping[str, Any]) -> dict:
    """
    执行前最后确认。路由只看 exec_decision，不借道 summary。

    参数:
        state: 读 ``exec_params`` / plans。

    返回:
        ``exec_decision`` 为 proceed 或 cancel；取消时附带 cancelled summary。
    """
    params = state.get("exec_params") or {}
    plans = list(params.get("plans") or [])
    decision = interrupt(
        {
            "type": "confirm_exec",
            "message": f"确认创建并启动 {len(plans)} 条流水线？输入 yes 继续，no 取消",
            "params": params,
            "plans": plans,
        }
    )

    text = str(decision).strip().lower()
    ok = text in {"yes", "y", "是", "确认", "true", "1"}

    if not ok:
        return {
            "exec_decision": "cancel",
            "summary": {
                "status": "cancelled",
                "message": "用户取消执行",
            },
            "audit": [{"step": "confirm_exec", "decision": "cancel"}],
        }

    return {
        "exec_decision": "proceed",
        "audit": [{"step": "confirm_exec", "decision": "proceed"}],
    }


def route_after_confirm(state: Mapping[str, Any]) -> str:
    """
    确认后条件边：proceed → create_pipelines；cancel → END。

    参数:
        state: 读 ``exec_decision``。

    返回:
        ``cancel`` 或 ``proceed``。
    """
    if state.get("exec_decision") == "cancel":
        return "cancel"
    return "proceed"
