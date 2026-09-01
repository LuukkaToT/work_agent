"""Prometheus 指标。标签只用量少的 op/status/code，不要 thread_id。

线上运营看这些；不要和 eval-diagnose / BFCL 分数画在同一张图上。
"""

from __future__ import annotations

from typing import Any, Mapping

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

TURN_LATENCY_BUCKETS = (0.5, 1, 2, 5, 10, 30, 60, 120, 300)
DIAGNOSE_LATENCY_BUCKETS = (0.5, 1, 2, 5, 10, 30, 60, 120)

turns_total = Counter(
    "work_agent_turns_total",
    "TurnService 调用次数（含失败）",
    ["op", "status", "code"],
)
turn_duration_seconds = Histogram(
    "work_agent_turn_duration_seconds",
    "TurnService 墙钟耗时",
    ["op"],
    buckets=TURN_LATENCY_BUCKETS,
)
thread_busy_total = Counter(
    "work_agent_thread_busy_total",
    "抢不到 thread 锁的次数",
    ["op"],
)
respond_fallback_total = Counter(
    "work_agent_respond_fallback_total",
    "respond.source=fallback 次数",
)
diagnose_latency_seconds = Histogram(
    "work_agent_diagnose_latency_seconds",
    "error_analysis.latency_ms 换算的秒",
    buckets=DIAGNOSE_LATENCY_BUCKETS,
)
llm_tokens_total = Counter(
    "work_agent_llm_tokens_total",
    "诊断 audit 里记录的 token（非评测准确率）",
    ["direction"],
)
http_unhandled_total = Counter(
    "work_agent_http_unhandled_total",
    "未捕获异常导致的 HTTP 500",
)


def observe_turn(
    *,
    op: str,
    outcome: str,
    status: str,
    code: str,
    duration_ms: int,
    summary: Mapping[str, Any],
) -> None:
    """从 TurnService 事件更新计数器。失败不影响主路径。"""
    status_label = status or ("error" if outcome == "failed" else "unknown")
    code_label = code or "unknown"
    turns_total.labels(op=op, status=status_label, code=code_label).inc()
    turn_duration_seconds.labels(op=op).observe(max(duration_ms, 0) / 1000.0)
    if code_label == "THREAD_BUSY":
        thread_busy_total.labels(op=op).inc()
    if summary.get("respond_source") == "fallback":
        respond_fallback_total.inc()
    diagnose = summary.get("diagnose") or {}
    latency_ms = diagnose.get("latency_ms")
    if isinstance(latency_ms, (int, float)) and latency_ms >= 0:
        diagnose_latency_seconds.observe(float(latency_ms) / 1000.0)
    usage = diagnose.get("token_usage") or {}
    if isinstance(usage, Mapping):
        for direction in ("input", "output", "total"):
            raw = usage.get(direction)
            if isinstance(raw, (int, float)) and raw > 0:
                llm_tokens_total.labels(direction=direction).inc(float(raw))


def render_latest() -> tuple[bytes, str]:
    """``/metrics`` 响应体与 Content-Type。"""
    return generate_latest(), CONTENT_TYPE_LATEST
