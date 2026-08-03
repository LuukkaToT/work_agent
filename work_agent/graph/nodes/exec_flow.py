"""
执行流节点：

  exec_params       抽出计划列表 + exec_mode（create_only | create_and_start）
  create_pipelines  逐计划 create；按需 start；写入台账
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal, Mapping, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from work_agent.core.config import get_settings
from work_agent.core.ledger import get_ledger
from work_agent.core.llm import get_chat_model
from work_agent.graph.nodes.context import conversation_context
from work_agent.tools.registry import get_pipeline_tool

ALLOWED_VERSIONS = frozenset({"27B", "27A", "26B", "26A"})
ExecMode = Literal["create_only", "create_and_start"]
_IP_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|[01]?\d?\d)$"
)


class ExecPlanOut(BaseModel):
    case_names: list[str] = Field(description="本条流水线要执行的用例名列表")
    version: Optional[str] = Field(
        default=None, description="版本：27B / 27A / 26B / 26A"
    )
    env: Optional[str] = Field(
        default=None,
        description="物理组网 IP，如 7.223.50.60；没说则 null",
    )


class ExecParamsOut(BaseModel):
    plans: list[ExecPlanOut] = Field(
        description=(
            "执行计划列表。同一环境批量用例合并为一条；"
            "不同环境（如 A 环境跑 X、B 环境跑 Y）拆成多条。"
        )
    )
    exec_mode: Literal["create_only", "create_and_start"] = Field(
        default="create_and_start",
        description=(
            "create_only=只创建不启动（用户说仅创建/先建别跑）；"
            "create_and_start=创建并启动（默认）"
        ),
    )


def _classify_env(env: str) -> str:
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
) -> dict[str, Any]:
    """将plan映射为dict，确认参数是否缺失，确实追加到missing里"""
    env_kind = _classify_env(env)
    missing: list[str] = []
    if not case_names:
        missing.append("case_names")
    if not env:
        missing.append("env")
    elif env_kind == "logical":
        missing.append("env")
    if version not in ALLOWED_VERSIONS:
        missing.append("version")
    return {
        "case_names": list(case_names),
        "version": version,
        "env": env,
        "env_kind": env_kind,
        "missing": missing,
    }


def exec_params(state: Mapping[str, Any]) -> dict:
    """
    参数优先级：
    - version / env：用户没说 → 留空，ask_missing interrupt
    - exec_mode：默认 create_and_start
    """
    llm = get_chat_model(temperature=0).with_structured_output(ExecParamsOut)

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
                    "从用户输入提取执行计划列表。"
                    "用例名通常很长，形如 HF_20B_PUSCH_..._MCS0_1_10_01 "
                    "或 TDD_26a_85_5002_..._KPI_TST，按原文提取，不要截断。"
                    "版本只能是 27B / 27A / 26B / 26A；本轮没明确说返回 null，"
                    "不要用默认值、不要从配置猜。"
                    "env 是物理组网 IP（如 7.223.50.60）；本轮没明确说返回 null，"
                    "不要从历史猜、也不要编造。"
                    "若用户说「A 环境执行 X，B 环境执行 Y」，拆成两条计划；"
                    "同一环境多个用例合并成一条，case_names 为列表。"
                    "本轮若是指代（如「再跑一遍」「换环境」），"
                    "结合【历史摘要】和【最近对话】补全用例名；"
                    "版本仅当历史里明确出现过才补，否则返回 null；"
                    "组网仍须本轮明确说出。"
                    "若用户说「只创建」「仅创建不用跑」「先建流水线别执行」，"
                    "exec_mode=create_only；否则 create_and_start。"
                )
            ),
            HumanMessage(content="\n".join(human_parts)),
        ]
    )

    plans: list[dict] = []
    for item in parsed.plans or []:
        raw = (item.version or "").strip()
        if not raw:
            version = ""
        elif raw.upper() in ALLOWED_VERSIONS:
            version = raw.upper()
        else:
            version = raw

        env = (item.env or "").strip()
        plans.append(
            _plan_dict(
                case_names=list(item.case_names or []),
                version=version,
                env=env,
            )
        )

    if not plans:
        plans = [_plan_dict(case_names=[], version="", env="")]

    exec_mode: ExecMode = parsed.exec_mode or "create_and_start"
    params = {"plans": plans, "exec_mode": exec_mode}
    return {
        "exec_params": params,
        "audit": [
            {
                "step": "exec_params",
                "plan_count": len(plans),
                "exec_mode": exec_mode,
                "params": params,
            }
        ],
    }


def _start_one(
    tool: Any,
    *,
    pipeline_id: str,
    retry_attempts: int,
) -> tuple[bool, list[str], str]:
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
    exec_mode: ExecMode,
    retry_attempts: int,
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
    )

    try:
        handle = tool.create(case_names, version, env)
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
    """逐计划 create；exec_mode=create_and_start 时再 start。单条失败不阻断。"""
    params = state.get("exec_params") or {}
    plans = list(params.get("plans") or [])
    exec_mode: ExecMode = params.get("exec_mode") or "create_and_start"
    task_id = state.get("task_id") or ""

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
        env = str(plan.get("env") or "").strip()

        if not case_names or not env or version not in ALLOWED_VERSIONS:
            pipelines.append(
                {
                    "pipeline_id": "",
                    "case_names": case_names,
                    "version": version,
                    "env": env,
                    "status": "failed",
                    "error": (
                        f"参数不完整: cases={bool(case_names)} "
                        f"version={version!r} env={env!r}"
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
            exec_mode=exec_mode,
            retry_attempts=retry_attempts,
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
    期望 state.pipelines 已由上游填好，或从 ledger 消解后写入。
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
