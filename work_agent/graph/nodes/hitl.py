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

from work_agent.core.capacity_mappings import lookup_capacity_topologies
from work_agent.graph.helpers.capacity_env import (
    format_capacity_group_message,
    format_logic_topology_multi_message,
    pick_capacity_group,
    pick_logic_topologies,
)
from work_agent.graph.helpers.logic_env import (
    apply_logic_catalog,
    format_logic_candidate_message,
    pick_logic_candidate,
)
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
            or reply.get("name")
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


def _expand_capacity_selections(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """完成“相似容量组单选 → 映射逻辑组网多选”，并把多选展开成计划。"""
    expanded: list[dict[str, Any]] = []
    for plan_index, original in enumerate(plans):
        plan = dict(original)
        group_candidates = list(plan.get("capacity_group_candidates") or [])
        if group_candidates:
            selected_group = None
            while selected_group is None:
                reply = interrupt(
                    {
                        "type": "pick_capacity_group",
                        "message": format_capacity_group_message(group_candidates),
                        "plan_index": plan_index,
                        "current": plan,
                        "candidates": group_candidates,
                        "multiple": False,
                    }
                )
                selected_group = pick_capacity_group(plan, reply)
            plan["capacity_ids"] = list(selected_group)
            plan["capacity_match"] = "fuzzy_confirmed"
            plan["logic_candidates"] = [
                record.as_choice()
                for record in lookup_capacity_topologies(selected_group)
            ]
            plan.pop("capacity_group_candidates", None)

        logic_candidates = list(plan.get("logic_candidates") or [])
        if plan.get("capacity_ids") and logic_candidates:
            selected: list[tuple[str, str]] = []
            while not selected:
                reply = interrupt(
                    {
                        "type": "pick_logic_topologies",
                        "message": format_logic_topology_multi_message(logic_candidates),
                        "plan_index": plan_index,
                        "current": plan,
                        "candidates": logic_candidates,
                        "multiple": True,
                    }
                )
                selected = pick_logic_topologies(plan, reply)
            for name, constraint in selected:
                item = _plan_dict(
                    case_names=list(plan.get("case_names") or []),
                    version=str(plan.get("version") or ""),
                    env=name,
                    logic_constraint=constraint,
                    capacity_ids=list(plan.get("capacity_ids") or []),
                )
                selected_choice = next(
                    (
                        choice
                        for choice in logic_candidates
                        if str(choice.get("name") or choice.get("logic_env") or "").strip()
                        == name
                        and str(
                            choice.get("constraint")
                            or choice.get("logic_constraint")
                            or ""
                        ).strip()
                        == constraint
                    ),
                    {},
                )
                item["logic_topology"] = {
                    "id": selected_choice.get("id"),
                    "name": name,
                    "constraint": constraint,
                    "config": selected_choice.get("config") or {"boards": []},
                }
                item["capacity_match"] = plan.get("capacity_match")
                expanded.append(item)
            continue
        expanded.append(plan)
    return expanded


def ask_missing(state: Mapping[str, Any]) -> dict:
    """
    按计划逐条补缺参（用例/版本/环境）。

    环境可填物理 IP，或完整逻辑组网（名称 + 约束）。

    参数:
        state: 读 ``exec_params.plans``（可缺省为空计划）。

    返回:
        补全后的 ``exec_params`` 与 audit；过程中可能多次 interrupt。
    """
    params = dict(state.get("exec_params") or {})
    plans = [dict(p) for p in (params.get("plans") or [])]
    if not plans:
        plans = [_plan_dict(case_names=[], version="", env="")]
    plans = _expand_capacity_selections(plans)

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
                candidates = list(plan.get("logic_candidates") or [])
                env_kind = plan.get("env_kind") or ""
                current_env = str(plan.get("env") or "").strip()
                if candidates:
                    msg = format_logic_candidate_message(
                        plan_index=idx + 1,
                        plan_count=len(plans),
                        candidates=candidates,
                    )
                    interrupt_type = "pick_logic_topology"
                elif env_kind == "logical" and current_env:
                    msg = (
                        f"第 {idx + 1}/{len(plans)} 条计划已有逻辑组网 `{current_env}`，"
                        "还缺约束。请补充约束（如 85+86），"
                        "或改填物理组网 IP（如 7.223.50.60）"
                    )
                    interrupt_type = "ask_env"
                else:
                    msg = (
                        f"第 {idx + 1}/{len(plans)} 条计划缺少环境。"
                        "请提供物理组网 IP（如 7.223.50.60），"
                        "或完整逻辑组网（名称 + 约束，"
                        "例如 3BBL_86_1BBL86 / 85+86）"
                    )
                    interrupt_type = "ask_env"
                reply = interrupt(
                    {
                        "type": interrupt_type,
                        "message": msg,
                        "plan_index": idx,
                        "current": plan,
                        "candidates": candidates,
                    }
                )
                picked = pick_logic_candidate(plan, reply)
                if picked:
                    env, constraint = picked
                else:
                    env, constraint = _apply_env_reply(plan, reply)
                plan["env"] = env
                plan["logic_constraint"] = constraint
                if env:
                    # 用户手工改填物理/直接逻辑组网时，放弃未命中的容量组。
                    plan["capacity_ids"] = []

            selected_topology = plan.get("logic_topology")
            rebuilt = _plan_dict(
                case_names=list(plan.get("case_names") or []),
                version=str(plan.get("version") or ""),
                env=str(plan.get("env") or ""),
                logic_constraint=str(plan.get("logic_constraint") or ""),
                capacity_ids=list(plan.get("capacity_ids") or []),
            )
            if plan.get("capacity_ids") and isinstance(selected_topology, dict):
                # 补用例/版本时保留容量映射选项携带的目录 id 与 JSON config。
                rebuilt["logic_topology"] = dict(selected_topology)
            gated = apply_logic_catalog([rebuilt], user_input="")[0]
            plan.update(gated)
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
