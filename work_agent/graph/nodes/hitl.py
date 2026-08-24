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

import json
import re
from typing import Any, Mapping

from langgraph.types import interrupt

from work_agent.graph.nodes.exec_flow import ALLOWED_VERSIONS, _classify_env, _plan_dict


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


def _parse_env_reply(reply: Any) -> tuple[str, str]:
    """把 HITL 回答解析成 (env, logic_constraint)。

    物理 IP 走物理模式；否则按「环境 / 约束」两段或 JSON 拆逻辑组网。
    单段非 IP 只当作 logic_env，约束留空。
    """
    if isinstance(reply, dict):
        physical = str(reply.get("physical_env") or "").strip()
        if physical:
            return physical, ""
        env = str(
            reply.get("env")
            or reply.get("logic_env")
            or reply.get("topology")
            or ""
        ).strip()
        constraint = str(
            reply.get("logic_constraint") or reply.get("constraint") or ""
        ).strip()
        return env, constraint

    text = str(reply or "").strip()
    if not text:
        return "", ""
    if text.startswith("{") and text.endswith("}"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return _parse_env_reply(data)

    if _classify_env(text) == "physical":
        return text, ""

    for sep in ("/", "，", ","):
        if sep in text:
            left, right = text.split(sep, 1)
            env, constraint = left.strip(), right.strip()
            if env or constraint:
                return env, constraint
    return text, ""


def _apply_env_reply(plan: Mapping[str, Any], reply: Any) -> tuple[str, str]:
    """把本次回答叠到当前计划上：可整段替换，也可只补约束。"""
    env, constraint = _parse_env_reply(reply)
    existing_env = str(plan.get("env") or "").strip()
    existing_kind = plan.get("env_kind") or _classify_env(existing_env)
    existing_constraint = str(plan.get("logic_constraint") or "").strip()

    if _classify_env(env) == "physical":
        return env, ""
    if (
        existing_kind == "logical"
        and existing_env
        and not existing_constraint
        and env
        and not constraint
        and env != existing_env
        and "+" in env
    ):
        return existing_env, env
    if not env and constraint and existing_kind == "logical" and existing_env:
        return existing_env, constraint
    if not env:
        env = existing_env
    if not constraint:
        constraint = existing_constraint if _classify_env(env) == "logical" else ""
    return env, constraint


def _parse_version(reply: Any) -> str:
    """把 HITL 回答解析成版本字符串（转大写）。"""
    if isinstance(reply, str):
        return reply.strip().upper()
    if isinstance(reply, dict):
        return str(reply.get("version") or "").strip().upper()
    return str(reply or "").strip().upper()


def ask_missing(state: Mapping[str, Any]) -> dict:
    """
    按计划逐条补缺参（用例/版本/环境）。

    环境可填物理 IP，或完整逻辑组网（逻辑环境 + 约束）。

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
                current_env = str(plan.get("env") or "").strip()
                if env_kind == "logical" and current_env:
                    msg = (
                        f"第 {idx + 1}/{len(plans)} 条计划已有逻辑组网 `{current_env}`，"
                        "还缺约束。请补充约束（如 85+86），"
                        "或改填物理组网 IP（如 7.223.50.60）"
                    )
                else:
                    msg = (
                        f"第 {idx + 1}/{len(plans)} 条计划缺少环境。"
                        "请提供物理组网 IP（如 7.223.50.60），"
                        "或完整逻辑组网（逻辑环境 + 约束，"
                        "例如 3BBL_86_1BBL86 / 85+86）"
                    )
                reply = interrupt(
                    {
                        "type": "ask_env",
                        "message": msg,
                        "plan_index": idx,
                        "current": plan,
                    }
                )
                env, constraint = _apply_env_reply(plan, reply)
                plan["env"] = env
                plan["logic_constraint"] = constraint

            rebuilt = _plan_dict(
                case_names=list(plan.get("case_names") or []),
                version=str(plan.get("version") or ""),
                env=str(plan.get("env") or ""),
                logic_constraint=str(plan.get("logic_constraint") or ""),
            )
            plan.update(rebuilt)
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
    # 为什么这里要 interrupt：core/policy.py 的
    # POLICIES["create_pipeline"].requires_confirmation 是 True（写操作，非只读）。
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
