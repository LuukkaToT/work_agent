"""容量 ID 候选挂载与 HITL 选择解析。"""

from __future__ import annotations

import re
from typing import Any

from work_agent.core.capacity_mappings import (
    find_similar_capacity_groups,
    lookup_capacity_topologies,
    normalize_capacity_ids,
)


def apply_capacity_catalog(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for original in plans:
        plan = dict(original)
        raw_ids = plan.get("capacity_ids") or []
        if not raw_ids:
            out.append(plan)
            continue
        group = normalize_capacity_ids(raw_ids)
        plan["capacity_ids"] = list(group)
        exact = lookup_capacity_topologies(group)
        if exact:
            plan["capacity_match"] = "exact"
            plan["logic_candidates"] = [record.as_choice() for record in exact]
            plan.pop("capacity_group_candidates", None)
        else:
            plan["capacity_match"] = "fuzzy"
            plan["capacity_group_candidates"] = [
                candidate.as_choice()
                for candidate in find_similar_capacity_groups(group)
            ]
            plan.pop("logic_candidates", None)
        missing = list(plan.get("missing") or [])
        if "env" not in missing:
            missing.append("env")
        plan["missing"] = missing
        out.append(plan)
    return out


def pick_capacity_group(plan: dict[str, Any], reply: Any) -> tuple[str, ...] | None:
    candidates = list(plan.get("capacity_group_candidates") or [])
    index: Any = reply.get("index") if isinstance(reply, dict) else reply
    try:
        selected = int(str(index).strip())
    except (TypeError, ValueError):
        return None
    if not 1 <= selected <= len(candidates):
        return None
    return normalize_capacity_ids(candidates[selected - 1].get("capacity_ids") or [])


def _selected_indices(reply: Any, count: int) -> list[int]:
    if isinstance(reply, dict):
        raw = reply.get("indices")
        if raw is None and reply.get("index") is not None:
            raw = [reply["index"]]
    elif isinstance(reply, list):
        raw = reply
    else:
        text = str(reply or "").strip()
        if text.casefold() in {"all", "全部"}:
            return list(range(count))
        raw = re.split(r"[,，\s]+", text)
    if not isinstance(raw, (list, tuple)):
        return []
    selected: list[int] = []
    for item in raw:
        try:
            index = int(item) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= index < count and index not in selected:
            selected.append(index)
    return selected


def pick_logic_topologies(plan: dict[str, Any], reply: Any) -> list[tuple[str, str]]:
    candidates = list(plan.get("logic_candidates") or [])
    pairs: list[tuple[str, str]] = []
    for index in _selected_indices(reply, len(candidates)):
        candidate = candidates[index]
        name = str(candidate.get("name") or candidate.get("logic_env") or "").strip()
        constraint = str(
            candidate.get("constraint") or candidate.get("logic_constraint") or ""
        ).strip()
        if name and constraint:
            pairs.append((name, constraint))
    return pairs


def format_capacity_group_message(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "没有找到相似容量 ID 组，请重新输入容量 ID、物理组网或完整逻辑组网。"
    lines = ["容量 ID 未精确匹配，请先确认最接近的容量组："]
    for index, candidate in enumerate(candidates, 1):
        ids = ", ".join(candidate.get("capacity_ids") or [])
        lines.append(f"  [{index}] {ids}（相似度 {candidate.get('score', 0):.0%}）")
    return "\n".join(lines)


def format_logic_topology_multi_message(candidates: list[dict[str, Any]]) -> str:
    lines = ["请选择一个或多个逻辑组网（例如 1,3；输入“全部”可全选）："]
    for index, candidate in enumerate(candidates, 1):
        display = candidate.get("display_name") or (
            f"{candidate.get('name') or candidate.get('logic_env')},"
            f"{candidate.get('constraint') or candidate.get('logic_constraint')}"
        )
        lines.append(f"  [{index}] {display}")
    return "\n".join(lines)
