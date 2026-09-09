"""单 ReAct 诊断的抽取上下文：原文事件摘录，普通淘汰块不调用摘要模型。"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from itertools import zip_longest

from work_agent.core.usage import TokenUsage
from work_agent.graph.helpers.context_archive import Archive, reference_note
from work_agent.graph.helpers.context_budget import PRIORITY_EVIDENCE, PRIORITY_RAG
from work_agent.graph.helpers.context_compressor import ContextCompressor, plain_excerpt
from work_agent.graph.helpers.context_manager import RenderResult
from work_agent.graph.helpers.context_selector import (
    ContextItem, ImmutableBudgetExceeded, PIN_IMMUTABLE, PIN_NORMAL, PIN_PROTECTED,
    normalize_items, render_items, select_within_budget,
)

_LOG_TOOLS = {"fetch_logs", "grep_logs", "fetch_archived_block"}
_SUPPORT_TOOLS = {"lookup_error_code", "search_knowledge", "find_case_history", "get_case_spec"}
# 单条日志进抽取窗口的默认上限。总预算约 7k，若干条 fetch/grep 不能各占 1600。
_LOG_EXCERPT_LIMIT = 800
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_FAULT = re.compile(
    r"error|warn|fail|exception|traceback|reject|timeout|refused|mismatch|invalid|missing|"
    r"unlocked|holdover|exhaust|not_ready|out_of_window|sequence_gap", re.I,
)
_RECOVERY = re.compile(
    r"\back\b|subscribe_ack|setupcomplete|established|recovered|recovery|"
    r"(?:cell_)?state=ACTIVE\b|RACH_COMPLETE|first_slot|verdict=PASS", re.I,
)
_REQUEST = re.compile(r"request|publish_announce|attempt=|retr(?:y|ies)|CONNECTING|WAITING", re.I)
_OMITTED = "[原文行省略]"


def _spread(indices: list[int]) -> list[int]:
    """同类事件优先首尾，避免连续重试挤掉初始和最终状态。"""
    result: list[int] = []
    left, right = 0, len(indices) - 1
    while left <= right:
        result.append(indices[left])
        if right != left:
            result.append(indices[right])
        left, right = left + 1, right - 1
    return result


def _render_lines(lines: list[str], selected: set[int]) -> str:
    parts: list[str] = []
    previous = -1
    for index in sorted(selected):
        if index > previous + 1:
            parts.append(_OMITTED)
        parts.append(lines[index])
        previous = index
    if previous < len(lines) - 1:
        parts.append(_OMITTED)
    return "\n".join(parts)


def log_excerpt(text: str, *, limit: int = _LOG_EXCERPT_LIMIT) -> str:
    """按原文整行选事件及邻行，保留时间顺序；不判断跨对象恢复或合并状态。"""
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    groups = [
        _spread([i for i, line in enumerate(lines) if pattern.search(line)])
        for pattern in (_FAULT, _RECOVERY, _REQUEST)
    ]
    # 三类轮流选取：异常不能占光预算，恢复也不自动覆盖较晚的异常。
    events = list(dict.fromkeys(
        index for row in zip_longest(*groups) for index in row if index is not None
    ))
    # 日志元信息可能是唯一包含文件/组件/流水线标识的行，不能只留正文。
    selected = {
        i for i, line in enumerate(lines)
        if line.startswith(("[log meta]", "[grep meta]"))
    }
    if len(_render_lines(lines, selected)) > limit:
        selected = set()
    order = events or _spread(list(range(len(lines))))
    for index in order:
        candidate = selected | {index}
        if len(_render_lines(lines, candidate)) <= limit:
            selected = candidate
    if events:
        # 只补事件邻行，不拿无关背景填满预算。
        for index in sorted(selected):
            for adjacent in (index - 1, index + 1):
                if 0 <= adjacent < len(lines):
                    candidate = selected | {adjacent}
                    if len(_render_lines(lines, candidate)) <= limit:
                        selected = candidate
    return _render_lines(lines, selected)


_READBACK_LIMIT = 800


def excerpt_archived_block(text: str, *, limit: int = _READBACK_LIMIT) -> str:
    """回读给模型的有界摘录。原文仍在 archive 里，这里不把 4k–8k 观察灌回下一轮。"""
    if len(text) <= limit:
        return text
    excerpted = log_excerpt(text, limit=limit)
    if excerpted.strip() in {"", _OMITTED} or len(excerpted) > limit:
        return plain_excerpt(text, limit)
    return excerpted


def _status_excerpt(text: str) -> str:
    """只整理实际返回的流水线身份、最终状态、用例 verdict 和失败说明。"""
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("status must be an object")
        status = {key: data[key] for key in ("pipeline_id", "phase", "message") if key in data}
        status["results"] = [
            {key: value for key, value in result.items() if key in {
                "case_name", "case_id", "verdict", "status", "message", "error", "fail_reason", "detail",
            }}
            for result in data.get("results", []) if isinstance(result, dict)
        ]
        return plain_excerpt(json.dumps(status, ensure_ascii=False), 600)
    except (ValueError, TypeError):
        return plain_excerpt(text, 600)


def _shrink_evidence(item: ContextItem, original: ContextItem, *, limit: int) -> str:
    """证据块只做确定性摘录，不交给摘要模型。"""
    source_text = original.text
    if item.source in _LOG_TOOLS:
        return log_excerpt(source_text, limit=limit)
    if item.source == "get_pipeline_status":
        body = _status_excerpt(source_text)
        return body if len(body) <= limit else plain_excerpt(body, limit)
    return plain_excerpt(source_text, limit)


def _project(item: ContextItem) -> ContextItem | None:
    if item.source == "list_log_files" and item.pin != PIN_IMMUTABLE:
        if "[list_log_files error]" not in item.text:
            return None
    if item.source in _LOG_TOOLS:
        if item.pin == PIN_IMMUTABLE:
            return item
        return replace(
            item,
            text=log_excerpt(item.text, limit=_LOG_EXCERPT_LIMIT),
            kind="evidence",
            priority=PRIORITY_EVIDENCE,
            pin=PIN_PROTECTED,
        )
    if item.source == "get_pipeline_status":
        if item.pin == PIN_IMMUTABLE:
            return item
        return replace(
            item,
            text=_status_excerpt(item.text),
            kind="evidence",
            priority=PRIORITY_EVIDENCE,
            pin=PIN_PROTECTED,
        )
    if item.pin != PIN_NORMAL:
        return item
    if item.source in _SUPPORT_TOOLS:
        return replace(
            item, text="[旁证，不能代替本案日志]\n" + plain_excerpt(item.text, 600),
            priority=PRIORITY_RAG,
        )
    return replace(item, text=plain_excerpt(item.text, 600))


def _dedup_selected_lines(items: list[ContextItem]) -> list[ContextItem]:
    """只去掉已入选块之间完全相同的带时间戳原文行。

    不去掉仅消息正文相同的跨时间/对象日志；不先对候选去重，以免其来源块
    随后被预算淘汰，而另一个块又因去重失去证据。
    """
    seen: set[str] = set()
    result: list[ContextItem] = []
    for item in items:
        if item.source not in _LOG_TOOLS:
            result.append(item)
            continue
        lines: list[str] = []
        for line in item.text.splitlines():
            if _TIMESTAMP.search(line):
                if line in seen:
                    continue
                seen.add(line)
            lines.append(line)
        result.append(replace(item, text="\n".join(lines)))
    return result


def render_diagnosis_context(
    items: list[ContextItem], *, goal: str, limit: int,
    compressor: ContextCompressor | None = None, archive: Archive | None = None,
) -> RenderResult:
    """抽取专用策略。普通块溢出仅归档；必保留块溢出最多一次批量摘要。"""
    originals = {item.item_id: item for item in items}
    suffixes: dict[str, str] = {}

    def archive_original(item_id: str) -> str:
        if archive is None:
            return ""
        if item_id not in suffixes:
            suffixes[item_id] = "\n" + reference_note(archive.store(originals[item_id]))
        return suffixes[item_id]

    candidates: list[ContextItem] = []
    seen: set[tuple[str, str]] = set()
    latest_status: dict[str, str] = {}
    for item in items:
        if item.source == "get_pipeline_status" and item.pin == PIN_NORMAL:
            try:
                data = json.loads(item.text)
                if isinstance(data, dict) and data.get("pipeline_id"):
                    latest_status[str(data["pipeline_id"])] = item.item_id
            except ValueError:
                pass  # 取证失败/截断文本不能代替此前成功读取的状态。
    for item in items:
        if item.source == "get_pipeline_status" and item.pin == PIN_NORMAL:
            try:
                data = json.loads(item.text)
                if (
                    isinstance(data, dict) and data.get("pipeline_id")
                    and latest_status[str(data["pipeline_id"])] != item.item_id
                ):
                    archive_original(item.item_id)
                    continue
            except ValueError:
                pass
        key = (item.source, item.text)
        if item.pin == PIN_NORMAL and key in seen:
            archive_original(item.item_id)
            continue
        seen.add(key)
        projected = _project(item)
        if projected is None:
            archive_original(item.item_id)
            continue
        if projected.text != item.text:
            projected = replace(projected, text=projected.text + archive_original(item.item_id))
        candidates.append(projected)

    normalized = normalize_items(candidates)
    first = select_within_budget(normalized, goal=goal, limit=limit)
    compressed_ids: list[str] = []
    degraded_ids: list[str] = []
    usage = TokenUsage()
    if first.needs_compression:
        protected = [item for item in normalized if item.pin == PIN_PROTECTED]
        immutable = [item for item in normalized if item.pin == PIN_IMMUTABLE]
        # 计算含块标题、分隔符、归档引用的真实空间，避免二次选择后仍超预算。
        reserved = sum(item.cost for item in immutable)
        overhead = sum(
            item.cost - len(item.text) + len(archive_original(item.item_id)) for item in protected
        )
        per_item = min(600, (limit - reserved - overhead) // len(protected))
        if per_item < 32:
            raise ImmutableBudgetExceeded("抽取预算无法容纳目标及必保留块的最小摘录与引用")
        evidence = [item for item in protected if item.kind == "evidence"]
        drafts = [item for item in protected if item.kind != "evidence"]
        replacements: dict[str, ContextItem] = {}
        for item in evidence:
            body = _shrink_evidence(item, originals.get(item.item_id, item), limit=per_item)
            suffix = archive_original(item.item_id)
            replacements[item.item_id] = replace(
                item, text=body if suffix in body else body + suffix,
            )
        long_drafts = [item for item in drafts if len(item.text) > per_item]
        if long_drafts:
            outcome = (compressor or ContextCompressor()).compress_batch(
                drafts, max_chars_each=per_item,
            )
            usage = outcome.usage
            degraded_ids = outcome.degraded_ids
            for item in outcome.items:
                suffix = archive_original(item.item_id)
                text = item.text if suffix in item.text else item.text + suffix
                replacements[item.item_id] = replace(item, text=text)
        compressed_ids = list(replacements)
        normalized = [replacements.get(item.item_id, item) for item in normalized]
        first = select_within_budget(normalized, goal=goal, limit=limit)
        if first.needs_compression:
            raise ImmutableBudgetExceeded("必保留块摘要仍超抽取预算")

    for item in first.dropped:
        archive_original(item.item_id)
    selected = _dedup_selected_lines(first.selected)
    for index, item in enumerate(selected):
        if item.text != first.selected[index].text:
            # 去重也是原文投影；确保未曾归档的来源仍可回溯。
            suffix = archive_original(item.item_id)
            if suffix and suffix not in item.text:
                selected[index] = replace(item, text=item.text + suffix)
    # 引用也占预算；淘汰最低优先级普通块，不能截断必保留块或伪造半行日志。
    final = select_within_budget(normalize_items(selected), goal=goal, limit=limit)
    for item in final.dropped:
        archive_original(item.item_id)
    text = render_items(final.selected)
    if len(text) > limit:
        raise ImmutableBudgetExceeded("最终抽取上下文超预算")
    return RenderResult(
        text=text, selected_ids=[item.item_id for item in final.selected], context_chars=len(text),
        usage=usage, compressed_ids=compressed_ids, degraded_ids=degraded_ids,
        archived=archive.refs if archive is not None else [], llm_used=usage.calls > 0,
    )
