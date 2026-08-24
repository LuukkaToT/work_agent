"""流水线 create 的环境模式：物理 IP 与逻辑组网+约束互斥。"""

from __future__ import annotations

from typing import Any, Literal, Mapping

EnvKind = Literal["physical", "logical"]


def resolve_create_env(
    *,
    physical_env: str | None = None,
    logic_env: str | None = None,
    logic_constraint: str | None = None,
) -> tuple[EnvKind, str, str]:
    """
    校验 create 的环境参数，返回 (env_kind, 展示用 env, logic_constraint)。

    物理模式只接受 ``physical_env``；逻辑模式必须同时有 ``logic_env`` 与
    ``logic_constraint``。两种都给、或都缺，抛 ``ValueError``。
    """
    physical = (physical_env or "").strip()
    logic = (logic_env or "").strip()
    constraint = (logic_constraint or "").strip()
    has_physical = bool(physical)
    has_logic_part = bool(logic or constraint)

    if has_physical and has_logic_part:
        raise ValueError("物理环境与逻辑组网+约束只能二选一")
    if has_physical:
        return "physical", physical, ""
    if logic and constraint:
        return "logical", logic, constraint
    if logic and not constraint:
        raise ValueError("逻辑组网缺少 logic_constraint")
    if constraint and not logic:
        raise ValueError("逻辑约束缺少 logic_env")
    raise ValueError("必须指定 physical_env，或同时指定 logic_env 与 logic_constraint")


def create_kwargs_from_plan(plan: Mapping[str, Any]) -> tuple[str, EnvKind, str, dict[str, str]]:
    """
    从执行计划抽出 create 关键字参数。

    返回:
        (展示 env, env_kind, logic_constraint, 传给 tool.create 的 kwargs)。

    异常:
        ValueError: 计划里的环境模式不完整或不合法。
    """
    env = str(plan.get("env") or "").strip()
    constraint = str(plan.get("logic_constraint") or "").strip()
    kind = str(plan.get("env_kind") or "").strip()
    if kind == "logical" or (kind != "physical" and env and constraint):
        resolved_kind, display, con = resolve_create_env(
            logic_env=env, logic_constraint=constraint
        )
        return display, resolved_kind, con, {
            "logic_env": display,
            "logic_constraint": con,
        }
    resolved_kind, display, con = resolve_create_env(physical_env=env)
    return display, resolved_kind, con, {"physical_env": display}
