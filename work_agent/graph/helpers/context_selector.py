"""
上下文选择：normalize → dedup → 相关性打分 → 预算选择。

本模块**不调 LLM、不碰磁盘**，全是纯函数，单测可以直接断言，不用 mock。
需要 LLM 的摘要在 ``context_compressor.py``，需要落盘的归档在
``context_archive.py``，三者的编排在 ``context_manager.py``。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Iterable, Literal

from work_agent.core.retrieval import tokenize
from work_agent.graph.helpers.context_budget import EVIDENCE_KEYWORDS

PinLevel = Literal["immutable", "protected", "normal"]

# 真正不可裁不可压：system prompt、当前目标。
PIN_IMMUTABLE: PinLevel = "immutable"
# 不允许删除，但允许压缩成摘要 + archive 引用。
PIN_PROTECTED: PinLevel = "protected"
# 按分数正常参与裁剪。
PIN_NORMAL: PinLevel = "normal"

# 打分权重。priority 的权重刻意远大于其它三项：它编码了
# conclusion > evidence > ruled_out > rag > raw 这个既有次序，不能被
# recency 之类的弱信号翻盘。
_W_PRIORITY = 10.0
_W_GOAL = 4.0
_W_EVIDENCE = 2.0
_W_RECENCY = 0.5

# 渲染成 【块名】\n 正文 的固定开销
_BLOCK_OVERHEAD = 8

_BLANK_RUN_RE = re.compile(r"\n{3,}")
_WS_RE = re.compile(r"\s+")
_CJK_START = "\u4e00"
_CJK_END = "\u9fff"


class ImmutableBudgetExceeded(RuntimeError):
    """
    immutable 项单独就超出预算。

    这不是「上下文太长」，而是 prompt 或预算配置写错了：真正不可裁的部分
    已经装不下，预算机制整体失效。显式抛出，不静默降级 —— 静默降级会让
    后续排查极难定位。
    """


@dataclass(frozen=True)
class ContextItem:
    """一块可参与裁剪的上下文。"""

    item_id: str  # 稳定 id，供 eval 记录 selected_context_ids
    kind: str  # tool_result | conclusion | evidence | ruled_out | rag | note
    source: str  # 工具名或来源
    text: str
    priority: int  # 复用 context_budget 的 PRIORITY_*；越小越重要
    pin: PinLevel = PIN_NORMAL
    seq: int = 0  # 加入顺序，recency 弱信号用；由 normalize_items 统一赋值

    @property
    def cost(self) -> int:
        """渲染进上下文后占用的字符数（含块名开销）。"""
        return len(self.text) + len(block_name(self)) + _BLOCK_OVERHEAD


@dataclass(frozen=True)
class SelectionResult:
    """一次预算选择的结果。"""

    selected: list[ContextItem]  # 最终保留（immutable + protected + 入选 normal）
    dropped: list[ContextItem]  # 被预算挤掉的 normal，按分数降序
    needs_compression: list[ContextItem]  # protected 但装不下，须交给 Compressor
    total_chars: int  # selected 的渲染成本合计

    @property
    def over_budget(self) -> bool:
        """是否需要进入压缩分支。"""
        return bool(self.needs_compression) or bool(self.dropped)


def block_name(item: ContextItem) -> str:
    """渲染时的块名；source 为空则只用 kind。"""
    return f"{item.kind}:{item.source}" if item.source else item.kind


def normalize_items(items: Iterable[ContextItem]) -> list[ContextItem]:
    """
    去掉空白项、收敛连续空行，并按输入顺序重排 ``seq``。

    ``seq`` 统一在这里赋值，所以 recency 的语义就是「加入上下文的先后」，
    调用方不需要自己维护。

    参数:
        items: 原始 item 序列。

    返回:
        归一后的列表；文本为空的项被丢掉。
    """
    out: list[ContextItem] = []
    for item in items:
        text = _BLANK_RUN_RE.sub("\n\n", (item.text or "").strip())
        if not text:
            continue
        out.append(replace(item, text=text, seq=len(out)))
    return out


def fingerprint(text: str) -> str:
    """忽略大小写与空白差异的内容指纹。"""
    flat = _WS_RE.sub(" ", (text or "").strip().lower())
    return hashlib.sha1(flat.encode("utf-8")).hexdigest()


def dedup_items(items: list[ContextItem]) -> tuple[list[ContextItem], list[ContextItem]]:
    """
    同源重复内容去重：先去完全重复，再去被包含的短文本。

    包含判断是真实需要的：``fetch_logs(tail=200)`` 之后再
    ``fetch_logs(tail=500)``，前者是后者的子串，留两份纯属浪费预算。

    参数:
        items: 已 normalize 的列表。

    返回:
        ``(保留项, 被去掉的项)``；保留项维持原有相对顺序。
    """
    seen: dict[tuple[str, str], ContextItem] = {}
    kept: list[ContextItem] = []
    removed: list[ContextItem] = []

    for item in items:
        key = (item.source, fingerprint(item.text))
        if key in seen:
            removed.append(item)
            continue
        seen[key] = item
        kept.append(item)

    flat = {i.item_id: _WS_RE.sub(" ", i.text.lower()) for i in kept}
    contained: set[str] = set()
    for a in kept:
        for b in kept:
            if a.item_id == b.item_id or a.source != b.source:
                continue
            if a.item_id in contained or b.item_id in contained:
                continue
            if len(flat[a.item_id]) < len(flat[b.item_id]) and flat[a.item_id] in flat[b.item_id]:
                contained.add(a.item_id)

    if not contained:
        return kept, removed
    final = [i for i in kept if i.item_id not in contained]
    removed.extend(i for i in kept if i.item_id in contained)
    return final, removed


def expand_tokens(text: str) -> set[str]:
    """
    分词并对中文串补二元组。

    ``retrieval.tokenize`` 把连续汉字当成一个 token，直接拿中文目标去比对
    几乎命中不了，所以这里额外切 bigram。
    """
    out: set[str] = set()
    for tok in tokenize(text):
        out.add(tok)
        if len(tok) > 1 and _CJK_START <= tok[0] <= _CJK_END:
            out.update(tok[i : i + 2] for i in range(len(tok) - 1))
    return out


def goal_relevance(text: str, goal_tokens: set[str]) -> float:
    """目标词元在本块中的覆盖率，值域 0..1。"""
    if not goal_tokens:
        return 0.0
    hit = goal_tokens & expand_tokens(text)
    return len(hit) / len(goal_tokens)


def evidence_bonus(text: str) -> float:
    """命中 ERROR / FAIL / Traceback 一类关键词则加分。"""
    low = (text or "").lower()
    return 1.0 if any(k.lower() in low for k in EVIDENCE_KEYWORDS) else 0.0


def score_item(item: ContextItem, *, goal_tokens: set[str], max_seq: int) -> float:
    """
    综合打分，越大越该留。

    ``score = -priority*10 + goal*4 + evidence*2 + recency*0.5``

    recency 只当弱信号：它一旦成为主裁剪依据，最早出现的证据会被系统性丢掉，
    而根因往往就在那儿。
    """
    recency = (item.seq / max_seq) if max_seq > 0 else 0.0
    return (
        -item.priority * _W_PRIORITY
        + goal_relevance(item.text, goal_tokens) * _W_GOAL
        + evidence_bonus(item.text) * _W_EVIDENCE
        + recency * _W_RECENCY
    )


def rank_items(items: list[ContextItem], *, goal: str) -> list[ContextItem]:
    """按分数降序排；同分用 seq 稳定兜底。"""
    goal_tokens = expand_tokens(goal)
    max_seq = max((i.seq for i in items), default=0)
    return sorted(
        items,
        key=lambda i: (-score_item(i, goal_tokens=goal_tokens, max_seq=max_seq), i.seq),
    )


def select_within_budget(
    items: list[ContextItem],
    *,
    goal: str,
    limit: int,
) -> SelectionResult:
    """
    在字符预算内挑选上下文。

    规则：
      - ``immutable`` 一律保留；单独超预算直接抛 ``ImmutableBudgetExceeded``。
      - ``protected`` 不允许删除；装不下时进 ``needs_compression`` 交给
        Compressor 压成摘要 + archive 引用，而不是丢掉。
      - ``normal`` 按分数降序贪心填充，装不下的进 ``dropped``（待归档）。

    参数:
        items: 已 normalize / dedup 的列表。
        goal: 当前诊断目标，用于相关性打分。
        limit: 总字符预算。

    返回:
        SelectionResult。

    异常:
        ImmutableBudgetExceeded: immutable 项合计已超预算。
    """
    immutable = [i for i in items if i.pin == PIN_IMMUTABLE]
    protected = [i for i in items if i.pin == PIN_PROTECTED]
    normal = [i for i in items if i.pin == PIN_NORMAL]

    immutable_cost = sum(i.cost for i in immutable)
    if immutable_cost > limit:
        raise ImmutableBudgetExceeded(
            f"immutable 上下文 {immutable_cost} 字符已超预算 {limit}；"
            "请检查 system prompt 或 react_history_max_chars 配置"
        )

    selected = list(immutable)
    used = immutable_cost
    needs_compression: list[ContextItem] = []

    protected_cost = sum(i.cost for i in protected)
    selected.extend(protected)
    used += protected_cost
    if used > limit:
        needs_compression = list(protected)

    dropped: list[ContextItem] = []
    for item in rank_items(normal, goal=goal):
        if used + item.cost <= limit:
            selected.append(item)
            used += item.cost
        else:
            dropped.append(item)

    selected.sort(key=lambda i: (i.priority, i.seq))
    return SelectionResult(
        selected=selected,
        dropped=dropped,
        needs_compression=needs_compression,
        total_chars=used,
    )


def select(
    items: Iterable[ContextItem],
    *,
    goal: str,
    limit: int,
) -> SelectionResult:
    """normalize → dedup → rank → budget 的一站式入口（各步也可单独调用）。"""
    normalized = normalize_items(items)
    kept, _removed = dedup_items(normalized)
    return select_within_budget(kept, goal=goal, limit=limit)


def render_items(items: list[ContextItem]) -> str:
    """
    渲染成 ``【块名】\\n正文`` 小节，格式与 ``assemble_blocks`` 一致。

    这里刻意不再做一次预算裁剪：预算已由 ``select_within_budget`` 保证，
    再截一次会让 ``selected_ids`` 与实际内容对不上，eval 记录就不可信了。
    """
    return "\n\n".join(f"【{block_name(i)}】\n{i.text.strip()}" for i in items)
