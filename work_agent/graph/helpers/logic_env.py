"""口头逻辑组网：快模型抽特征+提议参数，代码按目录校验 / 筛 HITL 候选。"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from work_agent.core.llm import get_fast_model
from work_agent.core.logic_topologies import (
    find_logic_topology_candidates,
    is_valid_logic_topology,
    lookup_logic_topology,
)


def _classify_env(env: str) -> str:
    from work_agent.graph.nodes.exec_flow import _classify_env as _cls

    return _cls(env)


def _plan_dict(**kwargs) -> dict[str, Any]:
    from work_agent.graph.nodes.exec_flow import _plan_dict as _pd

    return _pd(**kwargs)


_TOPOLOGY_HINT_RE = re.compile(
    r"BBH|BBL|BESA|逻辑组网|逻辑环境|G板|A板",
    re.IGNORECASE,
)


class TopologyUtteranceOut(BaseModel):
    """快模型对口头组网的一次结构化抽取。提议参数须经目录校验才可写入 plan。"""

    kind: Literal["physical", "logical", "unknown"] = Field(
        description="physical=物理 IP；logical=逻辑组网；unknown=看不出"
    )
    physical_env: Optional[str] = Field(
        default=None, description="物理 IP；不是则 null"
    )
    bbh_count: Optional[int] = Field(default=None, description="BBH 数量；没说则 null")
    bbl_count: Optional[int] = Field(default=None, description="BBL 数量；没说则 null")
    bbh_board: Optional[str] = Field(
        default=None, description="BBH 板型字母，如 G；没说则 null"
    )
    bbl_board: Optional[str] = Field(
        default=None, description="BBL 板型字母，如 A；没说则 null"
    )
    logic_env: Optional[str] = Field(
        default=None,
        description="提议的规范逻辑环境名，如 BESA_SDV_2BBH_1BBL；不确定则 null",
    )
    logic_constraint: Optional[str] = Field(
        default=None,
        description="提议的规范约束，如 1G_2A；不确定则 null",
    )


def parse_topology_utterance(text: str) -> TopologyUtteranceOut:
    """
    用快模型从本轮原话抽出组网特征与提议参数。不把目录表塞进 prompt。
    """
    utterance = (text or "").strip()
    if not utterance:
        return TopologyUtteranceOut(kind="unknown")
    llm = get_fast_model(temperature=0).with_structured_output(TopologyUtteranceOut)
    return llm.invoke(
        [
            SystemMessage(
                content=(
                    "从用户这句话判断测试环境。"
                    "若是 IPv4（如 7.223.142.119）kind=physical，填 physical_env。"
                    "若是逻辑组网（如 2BBH+1BBL、BBH 用 G 板、BBL 用 A 板），kind=logical，"
                    "抽出 bbh_count/bbl_count/bbh_board/bbl_board；"
                    "若能拼出规范名则填 logic_env（形如 BESA_SDV_2BBH_1BBL）"
                    "和 logic_constraint（形如 1G_2A 或 2G_1A），不确定就 null。"
                    "不要编造目录里不存在的前缀。看不出则 kind=unknown。"
                )
            ),
            HumanMessage(content=utterance),
        ]
    )


def _features_from_parsed(
    parsed: TopologyUtteranceOut | None,
) -> dict[str, Any]:
    if parsed is None or parsed.kind != "logical":
        return {}
    return {
        "bbh_count": parsed.bbh_count,
        "bbl_count": parsed.bbl_count,
        "bbh_board": (parsed.bbh_board or "").strip().upper() or "",
        "bbl_board": (parsed.bbl_board or "").strip().upper() or "",
    }


def _candidates_for(
    *,
    parsed: TopologyUtteranceOut | None,
    env_text: str,
) -> list[dict[str, Any]]:
    feats = _features_from_parsed(parsed)
    recs = find_logic_topology_candidates(
        bbh_count=feats.get("bbh_count"),
        bbl_count=feats.get("bbl_count"),
        bbh_board=feats.get("bbh_board") or "",
        bbl_board=feats.get("bbl_board") or "",
        text=env_text if _classify_env(env_text) == "logical" else "",
    )
    return [r.as_choice() for r in recs]


def _attach_candidates(plan: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    missing = list(plan.get("missing") or [])
    if "env" not in missing:
        missing.append("env")
    plan["missing"] = missing
    plan["logic_candidates"] = candidates
    return plan


def apply_logic_catalog(
    plans: list[dict[str, Any]],
    *,
    user_input: str,
) -> list[dict[str, Any]]:
    """
    物理 IP 原样放行。逻辑组网必须是目录里的 (env, constraint)。

    提议不在目录：按特征筛候选，留给 HITL 点选。
    """
    parsed: TopologyUtteranceOut | None = None
    need_parse = False
    for plan in plans:
        kind = plan.get("env_kind") or _classify_env(str(plan.get("env") or ""))
        env = str(plan.get("env") or "").strip()
        constraint = str(plan.get("logic_constraint") or "").strip()
        if kind == "physical":
            continue
        if kind == "logical" and is_valid_logic_topology(env, constraint):
            continue
        need_parse = True
        break
    if need_parse and _TOPOLOGY_HINT_RE.search(user_input or ""):
        parsed = parse_topology_utterance(user_input)

    out: list[dict[str, Any]] = []
    for plan in plans:
        out.append(_gate_one(plan, parsed=parsed, user_input=user_input))
    return out


def _gate_one(
    plan: dict[str, Any],
    *,
    parsed: TopologyUtteranceOut | None,
    user_input: str,  # 预留：按原话补检索；当前候选只用来自 parsed 的特征
) -> dict[str, Any]:
    case_names = list(plan.get("case_names") or [])
    version = str(plan.get("version") or "")
    env = str(plan.get("env") or "").strip()
    constraint = str(plan.get("logic_constraint") or "").strip()
    kind = _classify_env(env)

    if kind == "physical":
        gated = _plan_dict(
            case_names=case_names,
            version=version,
            env=env,
            logic_constraint="",
        )
        gated.pop("logic_candidates", None)
        return gated

    rec = lookup_logic_topology(env, constraint) if kind == "logical" else None
    if rec:
        gated = _plan_dict(
            case_names=case_names,
            version=version,
            env=rec.logic_env,
            logic_constraint=rec.logic_constraint,
        )
        gated.pop("logic_candidates", None)
        return gated

    if parsed is not None and parsed.kind == "physical":
        ip = (parsed.physical_env or "").strip()
        if _classify_env(ip) == "physical":
            gated = _plan_dict(
                case_names=case_names,
                version=version,
                env=ip,
                logic_constraint="",
            )
            gated.pop("logic_candidates", None)
            return gated

    if parsed is not None and parsed.kind == "logical":
        proposed_env = (parsed.logic_env or "").strip()
        proposed_con = (parsed.logic_constraint or "").strip()
        rec2 = lookup_logic_topology(proposed_env, proposed_con)
        if rec2:
            gated = _plan_dict(
                case_names=case_names,
                version=version,
                env=rec2.logic_env,
                logic_constraint=rec2.logic_constraint,
            )
            gated.pop("logic_candidates", None)
            return gated

    candidates = _candidates_for(parsed=parsed, env_text=env)
    gated = _plan_dict(
        case_names=case_names,
        version=version,
        env=env,
        logic_constraint=constraint,
    )
    return _attach_candidates(gated, candidates)


def pick_logic_candidate(
    plan: dict[str, Any], reply: Any
) -> tuple[str, str] | None:
    """若回答是候选序号或规范 (env, constraint)，返回该对；否则 None。"""
    candidates = list(plan.get("logic_candidates") or [])
    if isinstance(reply, dict):
        idx = reply.get("index")
        if idx is not None and candidates:
            try:
                i = int(idx)
            except (TypeError, ValueError):
                i = -1
            if 1 <= i <= len(candidates):
                choice = candidates[i - 1]
                return (
                    str(choice.get("logic_env") or "").strip(),
                    str(choice.get("logic_constraint") or "").strip(),
                )
        env = str(
            reply.get("logic_env") or reply.get("env") or ""
        ).strip()
        constraint = str(
            reply.get("logic_constraint") or reply.get("constraint") or ""
        ).strip()
        if env and constraint:
            return env, constraint

    text = str(reply or "").strip()
    if text.isdigit() and candidates:
        i = int(text)
        if 1 <= i <= len(candidates):
            choice = candidates[i - 1]
            return (
                str(choice.get("logic_env") or "").strip(),
                str(choice.get("logic_constraint") or "").strip(),
            )
    return None


def format_logic_candidate_message(
    *,
    plan_index: int,
    plan_count: int,
    candidates: list[dict[str, Any]],
) -> str:
    """HITL 点选文案。"""
    lines = [
        f"第 {plan_index}/{plan_count} 条计划的逻辑组网不在目录中，"
        "请选择下列规范组网序号，或改填物理组网 IP（如 7.223.142.119）："
    ]
    if candidates:
        for i, c in enumerate(candidates, 1):
            lines.append(
                f"  [{i}] {c.get('logic_env')} / {c.get('logic_constraint')}"
                f"  ({c.get('bbh_count')}BBH+{c.get('bbl_count')}BBL"
                f" BBH={c.get('bbh_board') or '?'} BBL={c.get('bbl_board') or '?'})"
            )
    else:
        lines.append("目录中没有符合特征的条目，请直接给出规范逻辑组网或物理 IP。")
    return "\n".join(lines)
