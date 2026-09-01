"""上线可观测：JSON 日志、request_id、audit 摘要。不泄漏用户原文或完整 audit。

REST 对外仍不返回 audit；本模块在 TurnService strip 之前把摘要打到 stdout，
供公司日志平台采集。CLI 默认不改 logging 配置，保持人读。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

_request_id: ContextVar[str] = ContextVar("work_agent_request_id", default="")
_event_hooks: list[Callable[[dict[str, Any]], None]] = []

logger = logging.getLogger("work_agent.obs")

_LOG_RECORD_SKIP = {
    "name",
    "msg",
    "args",
    "created",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
    "exc_info",
    "exc_text",
    "message",
}

# 按 step 白名单抽取；不包含 user_input / params / tool_trace / notes。
_STEP_KEYS: dict[str, tuple[str, ...]] = {
    "intake": ("task_id",),
    "router": ("intent",),
    "respond": ("source", "chars"),
    "create_pipelines": ("created", "failed", "status", "exec_mode"),
    "start_pipelines": ("started", "failed", "status"),
    "error_analysis": (
        "fail_kind",
        "latency_ms",
        "token_usage",
        "context_chars",
        "trimmed_steps",
        "status",
        "react_limit",
        "ruled_out_n",
        "context_strategy",
        "obs_compressed_n",
    ),
    "confirm_exec": ("decision",),
    "ask_missing": ("plan_count",),
    "memory": ("skipped",),
    "query_pipelines": ("status",),
    "resolve_pipelines": ("status", "ops_kind"),
    "init_ops_kind": ("ops_kind",),
    "exec_params": ("plan_count", "exec_mode"),
    "test_analysis": ("chars", "skill"),
    "quick_answer": ("chars",),
}


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象，方便任意日志平台刮 stdout。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": getattr(record, "event", None) or record.getMessage(),
        }
        rid = getattr(record, "request_id", None) or get_request_id()
        if rid:
            payload["request_id"] = rid
        for key, value in record.__dict__.items():
            if key in _LOG_RECORD_SKIP or key in payload or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def log_value(value: str | None, *, limit: int = 128) -> str:
    """截断并去掉换行，避免日志注入。"""
    return str(value or "").replace("\r", " ").replace("\n", " ")[:limit]


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(request_id: str):
    """绑定当前任务的 request_id；返回 token，结束时 reset。"""
    return _request_id.set((request_id or "").strip())


def reset_request_id(token) -> None:
    _request_id.reset(token)


def add_event_hook(hook: Callable[[dict[str, Any]], None]) -> None:
    """测试用：捕获 emit 出去的事件字典。"""
    _event_hooks.append(hook)


def clear_event_hooks() -> None:
    _event_hooks.clear()


def configure_logging(*, json_logs: bool | None = None, level: str | None = None) -> None:
    """
    给根 logger 加 stdout handler。不清除已有 handler（避免弄坏 pytest）。

    ``LOG_FORMAT=text`` 时用普通格式；默认 json（API 进程）。
    ``LOG_LEVEL`` 默认 INFO。
    """
    if json_logs is None:
        fmt = (os.getenv("LOG_FORMAT") or "json").strip().lower()
        json_logs = fmt != "text"
    raw_level = (level or os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    log_level = getattr(logging, raw_level, logging.INFO)

    root = logging.getLogger()
    already = any(getattr(handler, "_work_agent_obs", False) for handler in root.handlers)
    if not already:
        handler = logging.StreamHandler(sys.stdout)
        handler._work_agent_obs = True  # type: ignore[attr-defined]
        if json_logs:
            handler.setFormatter(JsonFormatter())
        else:
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
        root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > log_level:
        root.setLevel(log_level)


def summarize_audit(
    audit: Sequence[Any] | None,
    *,
    interrupt_types: Sequence[str] | None = None,
) -> dict[str, Any]:
    """从当轮 audit 抽出可观测摘要，不含用户原文和完整轨迹。"""
    steps: list[dict[str, Any]] = []
    for rec in audit or []:
        if not isinstance(rec, Mapping):
            continue
        step = str(rec.get("step") or "")
        if not step:
            continue
        item: dict[str, Any] = {"step": step}
        for key in _STEP_KEYS.get(step, ()):
            if key not in rec:
                continue
            item[key] = _clip_value(rec[key])
        error = rec.get("error")
        if error:
            item["error"] = log_value(str(error), limit=160)
        pids = rec.get("pipeline_ids")
        if isinstance(pids, list):
            item["pipeline_id_n"] = len(pids)
        steps.append(item)

    types = [log_value(t, limit=64) for t in (interrupt_types or []) if t]
    intent = next((s.get("intent") for s in steps if s.get("step") == "router"), None)
    respond_source = next(
        (s.get("source") for s in steps if s.get("step") == "respond"), None
    )
    diagnose = next((s for s in steps if s.get("step") == "error_analysis"), None)
    create = next((s for s in steps if s.get("step") == "create_pipelines"), None)
    start = next((s for s in steps if s.get("step") == "start_pipelines"), None)
    return {
        "steps": [s.get("step") for s in steps],
        "intent": intent,
        "respond_source": respond_source,
        "interrupt_types": types,
        "diagnose": diagnose,
        "create": create,
        "start": start,
        "records": steps,
    }


def emit_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """打一条结构化事件；同时通知测试 hook。"""
    payload: dict[str, Any] = {"event": event, **fields}
    rid = get_request_id()
    if rid:
        payload["request_id"] = rid
    for hook in list(_event_hooks):
        try:
            hook(dict(payload))
        except Exception:  # noqa: BLE001
            logger.debug("event hook failed", exc_info=True)
    extra = {k: v for k, v in payload.items() if k != "event"}
    logger.log(level, event, extra={"event": event, **extra})


def emit_turn_event(
    *,
    op: str,
    outcome: str,
    user_id: str,
    thread_id: str,
    duration_ms: int,
    status: str = "",
    code: str = "",
    runtime_result: Mapping[str, Any] | None = None,
    interrupt_types: Sequence[str] | None = None,
) -> dict[str, Any]:
    """turn / resume / status 完成或失败。outcome 为 complete 或 failed。"""
    audit = None
    if runtime_result is not None:
        audit = runtime_result.get("audit")
    summary = summarize_audit(audit, interrupt_types=interrupt_types)
    event = f"{op}_{outcome}"
    payload = {
        "op": op,
        "user_id": log_value(user_id),
        "thread_id": log_value(thread_id),
        "status": status,
        "code": code or ("ok" if outcome == "complete" else "INTERNAL"),
        "duration_ms": int(duration_ms),
        "intent": summary.get("intent"),
        "respond_source": summary.get("respond_source"),
        "interrupt_types": summary.get("interrupt_types") or [],
        "steps": summary.get("steps") or [],
        "diagnose": summary.get("diagnose"),
        "create": summary.get("create"),
        "start": summary.get("start"),
    }
    level = logging.INFO if outcome == "complete" else logging.WARNING
    if payload["code"] == "INTERNAL" and outcome == "failed":
        level = logging.ERROR
    emit_event(event, level=level, **payload)
    try:
        from work_agent.core.metrics import observe_turn

        observe_turn(
            op=op,
            outcome=outcome,
            status=status,
            code=str(payload["code"]),
            duration_ms=duration_ms,
            summary=summary,
        )
    except Exception:  # noqa: BLE001
        logger.debug("metrics observe failed", exc_info=True)
    return payload


def interrupt_types_from_result(result: Mapping[str, Any] | None) -> list[str]:
    """只取 interrupt 的 type，不要 message / 选项原文。"""
    if not result:
        return []
    items = result.get("__interrupt__") or []
    types: list[str] = []
    for item in items:
        value = getattr(item, "value", item)
        if isinstance(value, Mapping) and value.get("type"):
            types.append(str(value["type"]))
        elif isinstance(value, str):
            types.append(value[:64])
    return types


def _clip_value(value: Any) -> Any:
    if isinstance(value, str):
        return log_value(value, limit=120)
    if isinstance(value, dict):
        # token_usage 等小 dict 原样留下数值。
        return {
            str(k): v
            for k, v in value.items()
            if isinstance(v, (int, float, str, bool)) or v is None
        }
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return log_value(str(value), limit=80)
