"""
执行流节点：

  exec_params       从自然语言抽出执行计划列表（可多环境拆多条）
  create_pipelines  逐计划 init_pipline + check_pipline，写入台账

真实 tool 不轮询：提交完本轮结束，用户问进度时走 query_run。
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Mapping, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from work_agent.core.config import get_settings
from work_agent.core.ledger import get_ledger
from work_agent.core.llm import get_chat_model
from work_agent.graph.nodes.context import conversation_context
from work_agent.tools.registry import get_pipeline_tool

ALLOWED_VERSIONS = frozenset({"27B", "27A", "26B", "26A"})
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


def _classify_env(env: str) -> str:
    """physical | logical | ''（空）。"""
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
    env_kind = _classify_env(env)
    missing: list[str] = []
    if not case_names:
        missing.append("case_names")
    if not env:
        missing.append("env")
    elif env_kind == "logical":
        # 现阶段只支持物理 IP；逻辑组网记为缺失，由 ask_missing 提示
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
    - version：用户没说 → 留空，ask_missing interrupt
    - env：用户没说 → 留空，不允许静默填（跑错环境代价高）

    多环境拆多条计划；单环境多用例合并为一条。
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

    # 调用llm从user_input解析出执行的参数
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
            # 非法字面量保留，交给ask_missing
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
        plans = [
            _plan_dict(
                case_names=[],
                version="",
                env="",
            )
        ]

    params = {"plans": plans}
    return {
        "exec_params": params,
        "audit": [{"step": "exec_params", "plan_count": len(plans), "params": params}],
    }


def _ensure_pipeline_created(
    tool: Any,
    *,
    run_id: str,
    case_names: list[str],
    version: str,
    env: str,
    retry_attempts: int,
) -> tuple[bool, list[str], str]:
    """
    init_pipline + 超时对账 + 同 run_id 重试。

    返回 (ok, notes, last_error)。
    超时不等于失败：先 query_result 对账，查得到视为已创建。
    """
    notes: list[str] = []
    last_error = ""
    max_attempts = 1 + max(0, retry_attempts)

    for attempt in range(1, max_attempts + 1):
        try:
            tool.init_pipline(run_id, case_names, version, env)
            if attempt > 1:
                notes.append(f"init 第 {attempt} 次成功")
            return True, notes, ""
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            notes.append(f"init 第 {attempt} 次失败: {exc}")

        # 对账：可能服务端已创建，只是客户端超时
        try:
            tool.query_result(run_id)
            notes.append("对账发现已创建")
            return True, notes, ""
        except KeyError:
            notes.append("对账：run_id 尚不存在")
        except Exception as qexc:  # noqa: BLE001
            notes.append(f"对账异常: {qexc}")

    return False, notes, last_error or "init_pipline 失败"


def _ensure_pipeline_started(
    tool: Any,
    *,
    run_id: str,
    retry_attempts: int,
) -> tuple[bool, list[str], str]:
    """check_pipline 幂等重试。"""
    notes: list[str] = []
    last_error = ""
    max_attempts = 1 + max(0, retry_attempts)

    for attempt in range(1, max_attempts + 1):
        try:
            tool.check_pipline(run_id)
            if attempt > 1:
                notes.append(f"check 第 {attempt} 次成功")
            return True, notes, ""
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            notes.append(f"check 第 {attempt} 次失败: {exc}")

    return False, notes, last_error or "check_pipline 失败"


def _submit_one_pipeline(
    tool: Any,
    ledger: Any,
    *,
    run_id: str,
    task_id: str,
    case_names: list[str],
    version: str,
    env: str,
    retry_attempts: int,
) -> dict[str, Any]:
    """
    单条流水线提交：write-ahead → init（对账重试）→ check → 终态记账。
    """
    entry: dict[str, Any] = {
        "run_id": run_id,
        "case_names": case_names,
        "version": version,
        "env": env,
        "status": "pending",
        "error": "",
        "notes": [],
    }

    # write-ahead：进程崩在调用中途，台账仍留悬案可对账
    ledger.upsert(
        run_id=run_id,
        task_id=task_id,
        case_names=case_names,
        version=version,
        env=env,
        status="creating",
    )

    ok, notes, err = _ensure_pipeline_created(
        tool,
        run_id=run_id,
        case_names=case_names,
        version=version,
        env=env,
        retry_attempts=retry_attempts,
    )
    entry["notes"].extend(notes)
    if not ok:
        entry["status"] = "failed"
        entry["error"] = err
        ledger.update_status(run_id, status="failed")
        return entry

    ok, notes, err = _ensure_pipeline_started(
        tool, run_id=run_id, retry_attempts=retry_attempts
    )
    entry["notes"].extend(notes)
    if not ok:
        entry["status"] = "failed"
        entry["error"] = err
        ledger.update_status(run_id, status="failed")
        return entry

    entry["status"] = "running"
    ledger.update_status(run_id, status="running")
    return entry


def create_pipelines(state: Mapping[str, Any]) -> dict:
    """
    逐计划创建并启动流水线。单条失败不阻断其余。
    run_id 由 agent 生成后传入 init_pipline（幂等键）。
    """
    params = state.get("exec_params") or {}
    plans = list(params.get("plans") or [])
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
    created = 0
    failed_n = 0

    for plan in plans:
        case_names = list(plan.get("case_names") or [])
        version = str(plan.get("version") or "")
        env = str(plan.get("env") or "").strip()
        run_id = f"pipe-{uuid.uuid4().hex[:8]}"

        if not case_names or not env or version not in ALLOWED_VERSIONS:
            pipelines.append(
                {
                    "run_id": run_id,
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
            run_id=run_id,
            task_id=task_id,
            case_names=case_names,
            version=version,
            env=env,
            retry_attempts=retry_attempts,
        )
        if entry["status"] == "running":
            created += 1
        else:
            failed_n += 1
        pipelines.append(entry)

    if created and not failed_n:
        status = "submitted"
        message = (
            f"已创建并启动 {created} 条流水线，"
            "结果请到流水线前端查看，也可稍后问我进度"
        )
    elif created and failed_n:
        status = "partial"
        message = f"成功 {created} 条，失败 {failed_n} 条"
    else:
        status = "failed"
        message = f"全部 {failed_n} 条流水线创建失败"

    return {
        "pipelines": pipelines,
        "summary": {
            "status": status,
            "message": message,
            "created": created,
            "failed_pipelines": failed_n,
        },
        "audit": [
            {
                "step": "create_pipelines",
                "created": created,
                "failed": failed_n,
                "run_ids": [p["run_id"] for p in pipelines],
                "notes": {p["run_id"]: p.get("notes") or [] for p in pipelines},
            }
        ],
    }
