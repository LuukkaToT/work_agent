"""
执行流节点：

  exec_params       抽出计划列表 + exec_mode；可从 Excel 读用例
  create_pipelines  逐计划 create；按需 start；写入台账
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal, Mapping, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from work_agent.core.ci_cases import lookup_ci_case
from work_agent.core.config import get_settings
from work_agent.core.ledger import get_ledger
from work_agent.core.llm import get_fast_model
from work_agent.core.user_config import get_debug_mode, get_version_space
from work_agent.graph.helpers.context import conversation_context
from work_agent.graph.helpers.logic_env import apply_logic_catalog
from work_agent.graph.helpers.sheet_plans import (
    apply_column_mapping,
    mapping_prompt_payload,
)
from work_agent.tools.create_mode import create_kwargs_from_plan
from work_agent.tools.registry import get_case_sheet_tool, get_pipeline_tool

ALLOWED_VERSIONS = frozenset({"27B", "27A", "26B", "26A"})
ExecMode = Literal["create_only", "create_and_start"]
_IP_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|[01]?\d?\d)$"
)


class ExecPlanOut(BaseModel):
    case_names: list[str] = Field(
        default_factory=list, description="本条流水线要执行的用例路径列表"
    )
    version: Optional[str] = Field(
        default=None, description="版本：27B / 27A / 26B / 26A"
    )
    physical_env: Optional[str] = Field(
        default=None,
        description="物理组网 IP，如 7.223.50.60；没说则 null",
    )
    logic_env: Optional[str] = Field(
        default=None,
        description="逻辑组网，如 3BBL_86_1BBL86；没说则 null",
    )
    logic_constraint: Optional[str] = Field(
        default=None,
        description="逻辑约束，如 85+86；没说则 null",
    )


class ExecParamsOut(BaseModel):
    plans: list[ExecPlanOut] = Field(
        default_factory=list,
        description=(
            "执行计划列表。同一环境批量用例合并为一条；"
            "不同环境拆成多条。若用例来自表格，case_names 可留空。"
        ),
    )
    exec_mode: Literal["create_only", "create_and_start"] = Field(
        default="create_and_start",
        description=(
            "create_only=只创建不启动（用户说仅创建/先建别跑）；"
            "create_and_start=创建并启动（默认）"
        ),
    )
    sheet_path: Optional[str] = Field(
        default=None, description="本地 Excel/CSV 路径；没有则 null"
    )
    row_limit: Optional[int] = Field(
        default=None,
        description="只要表里前 N 条非空用例；没说则 null=全表（有上限）",
    )


class ColumnMappingOut(BaseModel):
    case_name_col: Optional[int] = Field(description="用例名列 0-based 下标")
    version_col: Optional[int] = Field(
        default=None, description="版本列下标；没有则 null"
    )
    env_col: Optional[int] = Field(
        default=None, description="环境列下标；没有则 null"
    )
    confidence: Literal["high", "low"] = Field(description="映射把握")
    reason: str = Field(description="一句话理由")


def _classify_env(env: str) -> str:
    """按字符串判断环境是 physical（IP）还是 logical。"""
    text = (env or "").strip()
    if not text:
        return ""
    if _IP_RE.match(text):
        return "physical"
    return "logical"


def _plan_dict(
    *,
    case_names: list[str],
    version: str,
    env: str,
    logic_constraint: str = "",
) -> dict[str, Any]:
    """把计划字段归一成 dict，并计算 missing / env_kind。

    物理 IP 单独就算环境完整。逻辑组网必须 ``env`` + ``logic_constraint`` 成对
    才算完整，不再把逻辑组网当成「必须改成物理 IP」。
    """
    env = (env or "").strip()
    logic_constraint = (logic_constraint or "").strip()
    env_kind = _classify_env(env)
    if env_kind == "physical":
        logic_constraint = ""
        env_complete = True
    elif env_kind == "logical":
        env_complete = bool(logic_constraint)
    else:
        env_complete = False
        logic_constraint = ""

    missing: list[str] = []
    if not case_names:
        missing.append("case_names")
    if not env_complete:
        missing.append("env")
    if version not in ALLOWED_VERSIONS:
        missing.append("version")
    return {
        "case_names": list(case_names),
        "version": version,
        "env": env,
        "env_kind": env_kind,
        "logic_constraint": logic_constraint,
        "missing": missing,
    }


def _normalize_version(raw: str) -> str:
    """规范化版本字符串；合法则转大写，非法原样返回。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if text.upper() in ALLOWED_VERSIONS:
        return text.upper()
    return text


def _spoken_env_from_item(item: ExecPlanOut) -> tuple[str, str]:
    """从结构化输出抽出 (env, logic_constraint)；物理 IP 优先于逻辑组网。"""
    physical = (item.physical_env or "").strip()
    if _classify_env(physical) == "physical":
        return physical, ""
    logic_env = (item.logic_env or "").strip()
    constraint = (item.logic_constraint or "").strip()
    if logic_env:
        return logic_env, constraint
    return "", constraint


def _resolve_version(
    spoken: str,
    ci_version: str,
    version_space: str | None,
) -> str:
    """口头 version → CI version → version_space。非法口头值不覆盖。"""
    spoken_norm = _normalize_version(spoken)
    if spoken_norm in ALLOWED_VERSIONS:
        return spoken_norm
    if spoken_norm:
        return spoken_norm
    ci_norm = _normalize_version(ci_version)
    if ci_norm in ALLOWED_VERSIONS:
        return ci_norm
    space = (version_space or "").strip()
    if space in ALLOWED_VERSIONS:
        return space
    return ""


def _resolve_env(
    spoken_env: str,
    spoken_constraint: str,
    ci_logic_env: str,
    ci_constraint: str,
) -> tuple[str, str]:
    """口头物理 IP 或完整逻辑组网优先；否则用 CI 的逻辑组网+约束。"""
    spoken_env = (spoken_env or "").strip()
    spoken_constraint = (spoken_constraint or "").strip()
    kind = _classify_env(spoken_env)
    if kind == "physical":
        return spoken_env, ""
    if kind == "logical" and spoken_constraint:
        return spoken_env, spoken_constraint
    if kind == "logical":
        # 口头只给了逻辑环境：约束可从 CI 补
        ci_c = (ci_constraint or "").strip()
        if ci_c:
            return spoken_env, ci_c
        return spoken_env, ""
    ci_env = (ci_logic_env or "").strip()
    ci_c = (ci_constraint or "").strip()
    if ci_env:
        return ci_env, ci_c
    return "", ""


def _fill_plans_from_ci_and_config(
    plans: list[dict[str, Any]],
    *,
    user_id: str,
) -> list[dict[str, Any]]:
    """
    按用例路径补 version / 环境，再按 (version, env_kind, env, constraint) 重分组。

    优先级：口头 → CI 表 → user_config.version_space（仅 version）。
    """
    version_space = get_version_space(user_id)
    groups: dict[tuple[str, str, str, str], list[str]] = {}
    empty_slots: list[dict[str, Any]] = []

    for plan in plans:
        names = [str(n).strip() for n in (plan.get("case_names") or []) if str(n).strip()]
        spoken_version = str(plan.get("version") or "")
        spoken_env = str(plan.get("env") or "").strip()
        spoken_constraint = str(plan.get("logic_constraint") or "").strip()

        if not names:
            version = _resolve_version(spoken_version, "", version_space)
            env, constraint = _resolve_env(
                spoken_env, spoken_constraint, "", ""
            )
            empty_slots.append(
                _plan_dict(
                    case_names=[],
                    version=version,
                    env=env,
                    logic_constraint=constraint,
                )
            )
            continue

        for name in names:
            rec = lookup_ci_case(name)
            ci_ver = rec.version if rec else ""
            ci_env = rec.logic_env if rec else ""
            ci_con = rec.logic_constraint if rec else ""
            version = _resolve_version(spoken_version, ci_ver, version_space)
            env, constraint = _resolve_env(
                spoken_env, spoken_constraint, ci_env, ci_con
            )
            key = (version, env, constraint, _classify_env(env))
            groups.setdefault(key, []).append(name)

    filled = [
        _plan_dict(
            case_names=case_names,
            version=version,
            env=env,
            logic_constraint=constraint,
        )
        for (version, env, constraint, _kind), case_names in groups.items()
    ]
    return filled + empty_slots


def _spoken_env_version(plans: list[dict]) -> tuple[str, str, str]:
    """从口头计划里抽出第一份非空 env / version / logic_constraint。"""
    env = ""
    version = ""
    constraint = ""
    for p in plans:
        if not env and p.get("env"):
            env = str(p.get("env") or "").strip()
        if not version and p.get("version"):
            version = _normalize_version(str(p.get("version") or ""))
        if not constraint and p.get("logic_constraint"):
            constraint = str(p.get("logic_constraint") or "").strip()
    return env, version, constraint


def _resolve_sheet_plans(
    *,
    sheet_path: str,
    row_limit: int | None,
    spoken_env: str,
    spoken_version: str,
    spoken_constraint: str = "",
) -> tuple[list[dict], dict]:
    """读表 → 列映射（可 HITL）→ 切片计划。返回 (raw_plans, audit_extra)。"""
    tool = get_case_sheet_tool()
    table = tool.read(sheet_path)
    if not table.headers:
        raise ValueError(f"表格无表头: {sheet_path}")

    mapper = get_fast_model(temperature=0).with_structured_output(ColumnMappingOut)
    mapping: ColumnMappingOut = mapper.invoke(
        [
            SystemMessage(
                content=(
                    "根据表头与样本行，判断哪一列是用例名、版本、环境 IP。"
                    "用例名列通常含 case / 用例 / 脚本 等字样，或单元格像长标识符。"
                    "版本列多为 27B/27A/26B/26A。环境列多为 IP。"
                    "没有对应列就返回 null。把握不足时 confidence=low。"
                )
            ),
            HumanMessage(content=mapping_prompt_payload(table)),
        ]
    )

    case_col = mapping.case_name_col
    version_col = mapping.version_col
    env_col = mapping.env_col
    confidence = mapping.confidence

    if case_col is None or confidence == "low":
        headers_show = ", ".join(f"[{i}]{h}" for i, h in enumerate(table.headers))
        reply = interrupt(
            {
                "type": "pick_sheet_column",
                "message": (
                    "无法可靠识别用例名列，请输入用例名列的下标数字（从 0 开始）。\n"
                    f"表头：{headers_show}"
                ),
                "headers": table.headers,
                "suggested": {
                    "case_name_col": case_col,
                    "version_col": version_col,
                    "env_col": env_col,
                    "reason": mapping.reason,
                },
            }
        )
        try:
            case_col = int(str(reply).strip())
        except ValueError as exc:
            raise ValueError(f"无效的列下标: {reply!r}") from exc

    raw_plans = apply_column_mapping(
        table,
        case_name_col=int(case_col),
        version_col=version_col,
        env_col=env_col,
        row_limit=row_limit,
        spoken_env=spoken_env,
        spoken_version=spoken_version,
        spoken_constraint=spoken_constraint,
    )
    audit = {
        "sheet_path": table.path or sheet_path,
        "row_limit": row_limit,
        "case_name_col": case_col,
        "version_col": version_col,
        "env_col": env_col,
        "confidence": confidence,
        "mapping_reason": mapping.reason,
        "plan_count": len(raw_plans),
        "case_count": sum(len(p.get("case_names") or []) for p in raw_plans),
    }
    return raw_plans, audit


def exec_params(state: Mapping[str, Any]) -> dict:
    """
    从用户话抽出执行计划列表与 exec_mode；可走 Excel 读用例。

    参数优先级：
    - version：口头 → CI 表 → user_config.version_space → 仍空则 ask_missing
    - 环境：口头物理 IP 或完整逻辑组网 → CI 表逻辑组网+约束 → 仍缺则 ask_missing
    - exec_mode：默认 create_and_start
    - sheet_path：有则读表组装 plans（用例名不经模型手抄）

    参数:
        state: 读 ``user_input`` 与对话上下文。

    返回:
        ``exec_params``（plans/exec_mode/sheet_*）与 audit；读表失败时附 need_input summary。
    """
    llm = get_fast_model(temperature=0).with_structured_output(ExecParamsOut)

    ctx = conversation_context(state, n=8)
    user_input = state.get("user_input") or ""
    human_parts = []
    if ctx:
        human_parts.append(ctx)
        human_parts.append("")
    human_parts.append("【本轮用户输入】")
    human_parts.append(user_input)

    parsed: ExecParamsOut = llm.invoke(
        [
            SystemMessage(
                content=(
                    "从用户输入提取执行计划。"
                    "用例名通常很长，形如 HF_20B_PUSCH_..._MCS0_1_10_01，"
                    "按原文提取，不要截断；若用例来自表格则 case_names 可为空列表。"
                    "版本只能是 27B / 27A / 26B / 26A；本轮没明确说返回 null。"
                    "物理组网 IP（如 7.223.50.60）填 physical_env；没说则 null。"
                    "逻辑组网（如 3BBL_86_1BBL86）填 logic_env，配套约束（如 85+86）"
                    "填 logic_constraint；没说则 null。物理 IP 和逻辑组网不要填进同一个字段。"
                    "若用户给了 Excel/CSV 路径，填写 sheet_path（尽量保留原路径）。"
                    "若说「前三个/只要前 N 条」，填写 row_limit=N；否则 null。"
                    "若用户说「A 环境执行 X，B 环境执行 Y」，拆成两条计划；"
                    "同一环境多个用例合并成一条。"
                    "若用户说「只创建」「仅创建不用跑」，exec_mode=create_only；"
                    "否则 create_and_start。"
                )
            ),
            HumanMessage(content="\n".join(human_parts)),
        ]
    )

    spoken_plans: list[dict] = []
    for item in parsed.plans or []:
        version = _normalize_version(item.version or "")
        env, constraint = _spoken_env_from_item(item)
        spoken_plans.append(
            {
                "case_names": list(item.case_names or []),
                "version": version,
                "env": env,
                "logic_constraint": constraint,
            }
        )

    sheet_path = (parsed.sheet_path or "").strip() or None
    row_limit = parsed.row_limit
    sheet_audit: dict | None = None

    if sheet_path:
        spoken_env, spoken_version, spoken_constraint = _spoken_env_version(
            spoken_plans
        )
        try:
            raw_plans, sheet_audit = _resolve_sheet_plans(
                sheet_path=sheet_path,
                row_limit=row_limit,
                spoken_env=spoken_env,
                spoken_version=spoken_version,
                spoken_constraint=spoken_constraint,
            )
        except Exception as exc:  # noqa: BLE001
            params = {
                "plans": [
                    _plan_dict(
                        case_names=[],
                        version="",
                        env=spoken_env,
                        logic_constraint=spoken_constraint,
                    )
                ],
                "exec_mode": parsed.exec_mode or "create_and_start",
                "sheet_path": sheet_path,
                "row_limit": row_limit,
                "sheet_error": str(exc),
            }
            return {
                "exec_params": params,
                "summary": {
                    "status": "need_input",
                    "message": f"读取用例表失败: {exc}",
                },
                "audit": [
                    {
                        "step": "exec_params",
                        "status": "sheet_error",
                        "error": str(exc),
                        "sheet_path": sheet_path,
                    }
                ],
            }
        plans = [
            {
                "case_names": list(p.get("case_names") or []),
                "version": _normalize_version(str(p.get("version") or "")),
                "env": str(p.get("env") or "").strip(),
                "logic_constraint": str(p.get("logic_constraint") or "").strip(),
            }
            for p in raw_plans
        ]
        if not plans:
            plans = [
                {
                    "case_names": [],
                    "version": spoken_version,
                    "env": spoken_env,
                    "logic_constraint": "",
                }
            ]
    else:
        plans = list(spoken_plans)
        if not plans:
            plans = [
                {
                    "case_names": [],
                    "version": "",
                    "env": "",
                    "logic_constraint": "",
                }
            ]

    user_id = state.get("user_id") or ""
    plans = _fill_plans_from_ci_and_config(plans, user_id=user_id)
    plans = apply_logic_catalog(plans, user_input=user_input)

    exec_mode: ExecMode = parsed.exec_mode or "create_and_start"
    params: dict[str, Any] = {
        "plans": plans,
        "exec_mode": exec_mode,
        "sheet_path": sheet_path,
        "row_limit": row_limit,
    }
    audit_rec: dict[str, Any] = {
        "step": "exec_params",
        "plan_count": len(plans),
        "exec_mode": exec_mode,
        "params": params,
    }
    if sheet_audit:
        audit_rec["sheet"] = sheet_audit

    return {
        "exec_params": params,
        "audit": [audit_rec],
    }


def _start_one(
    tool: Any,
    *,
    pipeline_id: str,
    retry_attempts: int,
) -> tuple[bool, list[str], str]:
    """对单条 pipeline 调用 start，失败按 retry_attempts 重试。"""
    notes: list[str] = []
    last_error = ""
    max_attempts = 1 + max(0, retry_attempts)
    for attempt in range(1, max_attempts + 1):
        try:
            tool.start(pipeline_id)
            if attempt > 1:
                notes.append(f"start 第 {attempt} 次成功")
            return True, notes, ""
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            notes.append(f"start 第 {attempt} 次失败: {exc}")
    return False, notes, last_error or "start 失败"


def _submit_one_pipeline(
    tool: Any,
    ledger: Any,
    *,
    task_id: str,
    case_names: list[str],
    version: str,
    env: str,
    env_kind: str,
    logic_constraint: str,
    create_kwargs: dict[str, str],
    exec_mode: ExecMode,
    retry_attempts: int,
    user_id: str = "",
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    write-ahead(local) → create（服务端返回 pipeline_id）→ 可选 start。
    create 失败不盲目重试，防双建。
    """
    local_id = f"local-{uuid.uuid4().hex[:8]}"
    entry: dict[str, Any] = {
        "pipeline_id": local_id,
        "case_names": case_names,
        "version": version,
        "env": env,
        "env_kind": env_kind,
        "logic_constraint": logic_constraint,
        "status": "pending",
        "error": "",
        "notes": [],
    }

    ledger.upsert(
        pipeline_id=local_id,
        task_id=task_id,
        case_names=case_names,
        version=version,
        env=env,
        status="creating",
        user_id=user_id,
    )

    try:
        handle = tool.create(
            case_names, version, options=options or {}, **create_kwargs
        )
    except Exception as exc:  # noqa: BLE001
        entry["status"] = "failed"
        entry["error"] = str(exc)
        entry["notes"].append(f"create 失败（不重试以防双建）: {exc}")
        ledger.update_status(local_id, status="failed")
        return entry

    pipeline_id = handle.pipeline_id
    entry["pipeline_id"] = pipeline_id
    entry["notes"].append("create 成功")
    ledger.replace_id(local_id, pipeline_id, status="created")

    if exec_mode == "create_only":
        entry["status"] = "created"
        return entry

    ok, notes, err = _start_one(
        tool, pipeline_id=pipeline_id, retry_attempts=retry_attempts
    )
    entry["notes"].extend(notes)
    if not ok:
        entry["status"] = "failed"
        entry["error"] = err
        ledger.update_status(pipeline_id, status="failed")
        return entry

    entry["status"] = "running"
    ledger.update_status(pipeline_id, status="running")
    return entry


def create_pipelines(state: Mapping[str, Any]) -> dict:
    """
    逐计划 create；exec_mode=create_and_start 时再 start。单条失败不阻断。

    参数:
        state: 读 ``exec_params`` / ``task_id`` / ``user_id``（写入台账时打标创建者）。
            调测模式不进 state：提交时按 ``user_id`` 点查 ``get_debug_mode``。

    返回:
        ``pipelines`` 列表与聚合 ``summary``（created/failed_pipelines 等）及 audit。
    """
    params = state.get("exec_params") or {}
    plans = list(params.get("plans") or [])
    exec_mode: ExecMode = params.get("exec_mode") or "create_and_start"
    task_id = state.get("task_id") or ""
    user_id = state.get("user_id") or ""

    if not plans:
        return {
            "pipelines": [],
            "summary": {
                "status": "need_input",
                "message": "没有可创建的执行计划",
                "created": 0,
                "failed_pipelines": 0,
            },
            "audit": [{"step": "create_pipelines", "status": "empty"}],
        }

    debug_mode = bool(get_debug_mode(user_id))
    options = {"debug_mode": debug_mode}

    get_pipeline_tool.cache_clear()
    tool = get_pipeline_tool(scenario="all_pass")
    ledger = get_ledger()
    retry_attempts = get_settings().profile.create_retry_attempts

    pipelines: list[dict] = []
    ok_n = 0
    failed_n = 0

    for plan in plans:
        case_names = list(plan.get("case_names") or [])
        version = str(plan.get("version") or "")
        try:
            env, env_kind, logic_constraint, create_kwargs = create_kwargs_from_plan(
                plan
            )
        except ValueError:
            env = str(plan.get("env") or "").strip()
            env_kind = str(plan.get("env_kind") or "")
            logic_constraint = str(plan.get("logic_constraint") or "").strip()
            create_kwargs = None

        if (
            not case_names
            or version not in ALLOWED_VERSIONS
            or create_kwargs is None
        ):
            pipelines.append(
                {
                    "pipeline_id": "",
                    "case_names": case_names,
                    "version": version,
                    "env": env,
                    "env_kind": env_kind,
                    "logic_constraint": logic_constraint,
                    "status": "failed",
                    "error": (
                        f"参数不完整: cases={bool(case_names)} "
                        f"version={version!r} env={env!r} "
                        f"env_kind={env_kind!r} constraint={logic_constraint!r}"
                    ),
                    "notes": [],
                }
            )
            failed_n += 1
            continue

        entry = _submit_one_pipeline(
            tool,
            ledger,
            task_id=task_id,
            case_names=case_names,
            version=version,
            env=env,
            env_kind=env_kind,
            logic_constraint=logic_constraint,
            create_kwargs=create_kwargs,
            exec_mode=exec_mode,
            retry_attempts=retry_attempts,
            user_id=user_id,
            options=options,
        )
        if entry["status"] in ("created", "running"):
            ok_n += 1
        else:
            failed_n += 1
        pipelines.append(entry)

    if ok_n and not failed_n:
        if exec_mode == "create_only":
            status = "created"
            message = (
                f"已创建 {ok_n} 条流水线（未启动），"
                "需要时可让我按 pipeline_id 启动"
            )
        else:
            status = "submitted"
            message = (
                f"已创建并启动 {ok_n} 条流水线，"
                "结果请到流水线前端查看，也可稍后问我进度"
            )
    elif ok_n and failed_n:
        status = "partial"
        message = f"成功 {ok_n} 条，失败 {failed_n} 条"
    else:
        status = "failed"
        message = f"全部 {failed_n} 条流水线失败"

    return {
        "pipelines": pipelines,
        "summary": {
            "status": status,
            "message": message,
            "created": ok_n,
            "failed_pipelines": failed_n,
            "exec_mode": exec_mode,
        },
        "audit": [
            {
                "step": "create_pipelines",
                "exec_mode": exec_mode,
                "created": ok_n,
                "failed": failed_n,
                "pipeline_ids": [p.get("pipeline_id") for p in pipelines],
                "notes": {
                    p.get("pipeline_id") or f"fail-{i}": p.get("notes") or []
                    for i, p in enumerate(pipelines)
                },
            }
        ],
    }


def start_pipelines(state: Mapping[str, Any]) -> dict:
    """
    对台账/state 中已有的 pipeline_id 逐个 start。

    参数:
        state: 读已消解的 ``pipelines``（须含有效 pipeline_id）。

    返回:
        更新后的 ``pipelines`` / ``summary`` 与 audit。
    """
    pipelines = list(state.get("pipelines") or [])
    if not pipelines:
        return {
            "summary": {
                "status": "not_found",
                "message": "没有可启动的流水线",
            },
            "audit": [{"step": "start_pipelines", "status": "empty"}],
        }

    tool = get_pipeline_tool(scenario="all_pass")
    ledger = get_ledger()
    retry_attempts = get_settings().profile.create_retry_attempts

    started = 0
    failed_n = 0
    out: list[dict] = []

    for item in pipelines:
        entry = dict(item)
        pid = str(entry.get("pipeline_id") or "").strip()
        if not pid or pid.startswith("local-"):
            entry["status"] = "failed"
            entry["error"] = "缺少有效 pipeline_id"
            failed_n += 1
            out.append(entry)
            continue

        ok, notes, err = _start_one(
            tool, pipeline_id=pid, retry_attempts=retry_attempts
        )
        entry["notes"] = list(entry.get("notes") or []) + notes
        if ok:
            entry["status"] = "running"
            entry["error"] = ""
            ledger.update_status(pid, status="running")
            started += 1
        else:
            entry["status"] = "failed"
            entry["error"] = err
            ledger.update_status(pid, status="failed")
            failed_n += 1
        out.append(entry)

    if started and not failed_n:
        status = "submitted"
        message = f"已启动 {started} 条流水线"
    elif started and failed_n:
        status = "partial"
        message = f"启动成功 {started} 条，失败 {failed_n} 条"
    else:
        status = "failed"
        message = f"全部 {failed_n} 条启动失败"

    return {
        "pipelines": out,
        "summary": {
            "status": status,
            "message": message,
            "created": started,
            "failed_pipelines": failed_n,
        },
        "audit": [
            {
                "step": "start_pipelines",
                "started": started,
                "failed": failed_n,
                "pipeline_ids": [p.get("pipeline_id") for p in out],
            }
        ],
    }
