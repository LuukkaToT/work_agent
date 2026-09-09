"""诊断冷启动：有界跨组件 grep，抽出错误码/ERROR 线索给主编排器。

不是全量扫日志，也不生成已证实假设。线索只作路由提示，结案仍靠后续组件调查。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from work_agent.graph.helpers.truncate import clip_text

_ERROR_PATTERN = r"\[ERROR\]|errorcode=|cascade=true|DEPENDENCY_FAILED"
_HANDSHAKE_PATTERN = r"request_subscribe|subscribe_ack"
_ERRORCODE_RE = re.compile(r"E-[A-Z]+-\d+", re.IGNORECASE)
_COMPONENT_TAG_RE = re.compile(
    r"\[(COMM|RAT|BBH|BBL|MARP|COMPARE)\s*\]",
    re.IGNORECASE,
)
_COMPONENT_FIELD_RE = re.compile(
    r"\bcomponent=(comm|rat|bbh|bbl|marp|compare)\b",
    re.IGNORECASE,
)
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[\d:.]+|\d{4}-\d{2}-\d{2} [\d:]+)")
_HIT_LINE_RE = re.compile(r"^>\s+\d+:\s+(.*)$")
_TAG_TO_ID = {
    "COMM": "comm",
    "RAT": "rat",
    "BBH": "bbh",
    "BBL": "bbl",
    "MARP": "marp",
    "COMPARE": "compare",
}
_OVERVIEW_MAX_CHARS = 1500
_MAX_CODE_LOOKUPS = 5
_MAX_HITS_IN_OVERVIEW = 12
_DEFAULT_MAX_MATCHES = 24


@dataclass
class FailureCue:
    """一条粗扫命中：来源组件、可选错误码、是否级联、短摘录。"""

    component: str
    timestamp: str
    errorcode: str
    cascade: bool
    excerpt: str


@dataclass
class HandshakeHint:
    """握手空转线索：有 request_subscribe、未见 subscribe_ack。"""

    component: str
    request_subscribe: int
    subscribe_ack: int


@dataclass
class FailureCueScan:
    """一次冷启动粗扫的结构化结果，供拼进 overview。"""

    status: str = "ok"
    reason: str = ""
    hits: list[FailureCue] = field(default_factory=list)
    routing_hints: list[dict[str, Any]] = field(default_factory=list)
    handshake: list[HandshakeHint] = field(default_factory=list)

    def as_overview(self) -> str:
        """
        收成主编排器可读的 ``## failure_cues`` 段，约 1500 字以内。

        返回:
            带标题的纯文本；失败时仍含 ``scan_status=scan_failed``。
        """
        lines = ["## failure_cues", f"scan_status={self.status}"]
        if self.reason:
            lines.append(f"reason={self.reason}")
        if self.status != "ok":
            return clip_text("\n".join(lines), max_chars=_OVERVIEW_MAX_CHARS)
        lines.append(
            "note=cues 与 probable_components 是路由提示，不是根因；"
            "cascade=true 优先查上游。不得据此全查六组件。"
        )
        lines.append("hits:")
        shown = self.hits[:_MAX_HITS_IN_OVERVIEW]
        if not shown:
            lines.append("- （无 ERROR / errorcode 命中）")
        for item in shown:
            flag = "true" if item.cascade else "false"
            code = item.errorcode or "missing"
            ts = item.timestamp or "missing"
            lines.append(
                f"- ts={ts} component={item.component} errorcode={code} "
                f"cascade={flag} excerpt={item.excerpt}"
            )
        if len(self.hits) > len(shown):
            lines.append(f"- … 另有 {len(self.hits) - len(shown)} 条已去重命中未展开")
        lines.append("routing_hints:")
        if not self.routing_hints:
            lines.append("- （未查到错误码目录）")
        for hint in self.routing_hints:
            comps = ",".join(str(c) for c in hint.get("probable_components") or [])
            lines.append(
                f"- code={hint.get('code')} summary={hint.get('summary')} "
                f"is_root_code={hint.get('is_root_code')} "
                f"probable_components=[{comps}] note=路由提示不是根因"
            )
        lines.append("handshake:")
        if not self.handshake:
            lines.append("- （无请求无 ack 线索）")
        for item in self.handshake:
            lines.append(
                f"- component={item.component} request_subscribe={item.request_subscribe} "
                f"subscribe_ack={item.subscribe_ack} note=有请求无 ack（线索）"
            )
        return clip_text("\n".join(lines), max_chars=_OVERVIEW_MAX_CHARS)


def scan_failure_cues(
    pipeline_id: str,
    *,
    tools: Mapping[str, Any],
    max_matches: int = _DEFAULT_MAX_MATCHES,
    on_tool_start: Callable[[], None] | None = None,
) -> tuple[str, int, list[dict[str, Any]]]:
    """
    有界跨组件 grep，抽出失败线索；工具失败不抛给诊断主循环。

    参数:
        pipeline_id: 已消解的流水线。
        tools: ``build_diagnose_tools`` 得到的 name→tool；缺工具记失败。
        max_matches: grep 命中上限。
        on_tool_start: 每次真正 invoke 前记账。

    返回:
        ``(overview 文本, 工具调用次数, 轨迹)``。
    """
    trace: list[dict[str, Any]] = []
    calls = 0
    pid = (pipeline_id or "").strip()
    if not pid:
        scan = FailureCueScan(status="scan_failed", reason="pipeline_id 为空")
        return scan.as_overview(), 0, trace

    error_text, calls = _invoke(
        tools,
        "grep_logs",
        {
            "pipeline_id": pid,
            "pattern": _ERROR_PATTERN,
            "context_lines": 0,
            "max_matches": max(1, int(max_matches)),
            "component": "",
        },
        on_tool_start=on_tool_start,
        trace=trace,
        calls=calls,
    )
    if _tool_failed("grep_logs", error_text):
        scan = FailureCueScan(
            status="scan_failed",
            reason=clip_text(error_text, max_chars=240),
        )
        return scan.as_overview(), calls, trace

    hits = _dedupe_cues(_cues_from_grep(error_text))
    codes = _unique_errorcodes(hits)[:_MAX_CODE_LOOKUPS]
    routing: list[dict[str, Any]] = []
    for code in codes:
        lookup_text, calls = _invoke(
            tools,
            "lookup_error_code",
            {"code": code},
            on_tool_start=on_tool_start,
            trace=trace,
            calls=calls,
        )
        hint = _parse_lookup(lookup_text)
        if hint is not None:
            routing.append(hint)

    handshake_text, calls = _invoke(
        tools,
        "grep_logs",
        {
            "pipeline_id": pid,
            "pattern": _HANDSHAKE_PATTERN,
            "context_lines": 0,
            "max_matches": max(1, int(max_matches)),
            "component": "",
        },
        on_tool_start=on_tool_start,
        trace=trace,
        calls=calls,
    )
    handshake: list[HandshakeHint] = []
    if not _tool_failed("grep_logs", handshake_text):
        handshake = _handshake_hints(_cues_from_grep(handshake_text), handshake_text)

    scan = FailureCueScan(
        status="ok",
        hits=hits,
        routing_hints=routing,
        handshake=handshake,
    )
    return scan.as_overview(), calls, trace


def _invoke(
    tools: Mapping[str, Any],
    name: str,
    args: dict[str, Any],
    *,
    on_tool_start: Callable[[], None] | None,
    trace: list[dict[str, Any]],
    calls: int,
) -> tuple[str, int]:
    """调用白名单工具；缺注册或抛错收成 ``[name error]`` 字符串。"""
    tool = tools.get(name)
    trace.append(
        {
            "type": "call",
            "name": name,
            "args_preview": json.dumps(args, ensure_ascii=False),
        }
    )
    if tool is None:
        text = f"[{name} error] 工具未注册"
        trace.append(
            {
                "type": "result",
                "name": name,
                "content_chars": len(text),
                "status": "error",
            }
        )
        return text, calls
    if on_tool_start is not None:
        on_tool_start()
    try:
        text = str(tool.invoke(args))
        status = "error" if _tool_failed(name, text) else "success"
    except Exception as exc:  # noqa: BLE001
        text = f"[{name} error] {exc}"
        status = "error"
    calls += 1
    trace.append(
        {
            "type": "result",
            "name": name,
            "content_chars": len(text),
            "status": status,
        }
    )
    return text, calls


def _tool_failed(name: str, text: str) -> bool:
    """工具返回是否是错误串（含 real 后端 stub）。"""
    head = (text or "").lstrip()
    return head.startswith(f"[{name} error]") or head.startswith("[grep error]")


def _cues_from_grep(text: str) -> list[FailureCue]:
    """从 grep 输出里抽出 ``> 行号: 正文`` 命中。"""
    out: list[FailureCue] = []
    for raw in (text or "").splitlines():
        matched = _HIT_LINE_RE.match(raw)
        if matched is None:
            continue
        parsed = _parse_log_line(matched.group(1))
        if parsed is not None:
            out.append(parsed)
    return out


def _parse_log_line(line: str) -> FailureCue | None:
    """把一行日志收成线索；空行丢弃。"""
    text = (line or "").strip()
    if not text:
        return None
    tag = _COMPONENT_TAG_RE.search(text)
    field = _COMPONENT_FIELD_RE.search(text)
    component = "unknown"
    if tag is not None:
        component = _TAG_TO_ID.get(tag.group(1).upper(), "unknown")
    elif field is not None:
        component = field.group(1).casefold()
    ts_match = _TS_RE.match(text)
    timestamp = ts_match.group(1) if ts_match else ""
    codes = _ERRORCODE_RE.findall(text)
    errorcode = codes[0].upper() if codes else ""
    folded = text.casefold()
    cascade = "cascade=true" in folded or "dependency_failed" in folded
    excerpt = clip_text(" ".join(text.split()), max_chars=180)
    return FailureCue(
        component=component,
        timestamp=timestamp,
        errorcode=errorcode,
        cascade=cascade,
        excerpt=excerpt,
    )


def _dedupe_cues(hits: Sequence[FailureCue]) -> list[FailureCue]:
    """相同 errorcode+component（无码则用摘录键）只留最早一条。"""
    seen: set[tuple[str, str]] = set()
    out: list[FailureCue] = []
    for item in hits:
        key = (item.component, item.errorcode or _excerpt_key(item.excerpt))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _excerpt_key(excerpt: str) -> str:
    """去掉时间戳后的摘录，给无错误码的噪声 ERROR 去重。"""
    text = _TS_RE.sub("", excerpt or "", count=1).strip()
    return text[:80]


def _unique_errorcodes(hits: list[FailureCue]) -> list[str]:
    """按首次出现顺序收集错误码。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in hits:
        code = (item.errorcode or "").strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def _parse_lookup(text: str) -> dict[str, Any] | None:
    """把 ``lookup_error_code`` JSON 收成一条路由提示。"""
    if _tool_failed("lookup_error_code", text or ""):
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    codes = data.get("codes") if isinstance(data, dict) else None
    if not isinstance(codes, list) or not codes:
        return None
    item = codes[0]
    if not isinstance(item, dict):
        return None
    comps = [str(value).strip() for value in (item.get("probable_components") or []) if str(value).strip()]
    return {
        "code": str(item.get("code") or "").strip(),
        "summary": str(item.get("summary") or "").strip(),
        "is_root_code": item.get("is_root_code"),
        "probable_components": comps,
    }


def _handshake_hints(cues: list[FailureCue], raw: str) -> list[HandshakeHint]:
    """按组件统计 request_subscribe / subscribe_ack；只保留有请求无 ack。"""
    requests: dict[str, int] = {}
    acks: dict[str, int] = {}
    lines = [item.excerpt for item in cues]
    if not lines:
        lines = [
            matched.group(1)
            for raw_line in (raw or "").splitlines()
            if (matched := _HIT_LINE_RE.match(raw_line))
        ]
    for excerpt in lines:
        folded = excerpt.casefold()
        component = "unknown"
        tag = _COMPONENT_TAG_RE.search(excerpt)
        field = _COMPONENT_FIELD_RE.search(excerpt)
        if tag is not None:
            component = _TAG_TO_ID.get(tag.group(1).upper(), "unknown")
        elif field is not None:
            component = field.group(1).casefold()
        if "request_subscribe" in folded:
            requests[component] = requests.get(component, 0) + 1
        if "subscribe_ack" in folded and "subscription_ack_missing" not in folded:
            acks[component] = acks.get(component, 0) + 1
    hints: list[HandshakeHint] = []
    for component in sorted(set(requests) | set(acks)):
        req = requests.get(component, 0)
        ack = acks.get(component, 0)
        if req > 0 and ack == 0:
            hints.append(
                HandshakeHint(
                    component=component,
                    request_subscribe=req,
                    subscribe_ack=ack,
                )
            )
    return hints
