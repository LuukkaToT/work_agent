"""
上下文编排：把 Selector / Compressor / Archive 串起来。

本模块只负责编排，算法在 Selector、LLM 在 Compressor、写盘在 Archive。
处理链刻意让摘要后置，装得下就是零 LLM、零 IO：

    Normalize → Dedup → Rank → Budget
        ├─ 装得下 → Render
        └─ 装不下 → Compress → Re-budget → Archive discarded → Render
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from work_agent.core.usage import TokenUsage
from work_agent.graph.helpers.context_archive import ArchiveRef, ContextArchive, reference_note
from work_agent.graph.helpers.context_compressor import ContextCompressor
from work_agent.graph.helpers.context_selector import (
    ContextItem,
    render_items,
    select,
)

_DEFAULT_MAX_COMPRESS_ITEMS = 4
_DEFAULT_SUMMARY_MAX_CHARS = 600


@dataclass(frozen=True)
class RenderResult:
    """一次上下文渲染的产物与代价。"""

    text: str
    selected_ids: list[str]  # 进入最终上下文的 item_id，供 eval 记录
    context_chars: int  # 实际渲染出的字符数，A/B 的核心指标
    usage: TokenUsage = field(default_factory=TokenUsage)  # 压缩自身的 token 开销
    compressed_ids: list[str] = field(default_factory=list)
    degraded_ids: list[str] = field(default_factory=list)  # 摘要失败降级的块
    archived: list[ArchiveRef] = field(default_factory=list)
    llm_used: bool = False


class ContextManager:
    """按预算组装 working context，超预算时压缩 + 归档而不是静默丢弃。"""

    def __init__(
        self,
        *,
        compressor: ContextCompressor | None = None,
        archive: ContextArchive | None = None,
        max_compress_items: int = _DEFAULT_MAX_COMPRESS_ITEMS,
        summary_max_chars: int = _DEFAULT_SUMMARY_MAX_CHARS,
    ) -> None:
        """
        参数:
            compressor: 摘要器；None 用默认 ``ContextCompressor``。
            archive: 归档器；None 表示不归档（被裁内容直接丢）。诊断链路应传，
                这样才有 external context 可回溯。
            max_compress_items: 单次最多压缩几块，防止 LLM 调用发散。
            summary_max_chars: 每块压缩后的字符上限。
        """
        self._compressor = compressor or ContextCompressor()
        self._archive = archive
        self._max_compress_items = max(1, max_compress_items)
        self._summary_max_chars = max(80, summary_max_chars)

    def render(
        self,
        items: Iterable[ContextItem],
        *,
        goal: str,
        limit: int,
    ) -> RenderResult:
        """
        选出并渲染 working context。

        参数:
            items: 候选上下文块。
            goal: 当前诊断目标，用于相关性打分。
            limit: 字符预算。

        返回:
            RenderResult；``selected_ids`` 与 ``text`` 严格对应。

        异常:
            ImmutableBudgetExceeded: immutable 块单独超预算（配置错误）。
        """
        first = select(items, goal=goal, limit=limit)
        if not first.over_budget:
            text = render_items(first.selected)
            return RenderResult(
                text=text,
                selected_ids=[i.item_id for i in first.selected],
                context_chars=len(text),
            )

        targets = list(first.needs_compression)
        room = max(0, self._max_compress_items - len(targets))
        # 被挤掉的里分数最高的几条，压成摘要塞回去，而不是整条丢掉
        rescued = first.dropped[:room]
        targets.extend(rescued)

        usage = TokenUsage()
        degraded: list[str] = []
        compressed: list[ContextItem] = []
        for item in targets:
            suffix = ""
            if self._archive is not None:
                suffix = reference_note(self._archive.store(item))
            outcome = self._compressor.compress(
                [item], max_chars_each=self._summary_max_chars, suffix=suffix
            )
            usage = usage + outcome.usage
            degraded.extend(outcome.degraded_ids)
            compressed.extend(outcome.items)

        target_ids = {i.item_id for i in targets}
        survivors = [i for i in first.selected if i.item_id not in target_ids]
        second = select(survivors + compressed, goal=goal, limit=limit)

        if self._archive is not None:
            # 没被救回来的、以及二次仍装不下的，全部归档：不静默丢信息
            self._archive.store_all(
                [i for i in first.dropped if i.item_id not in target_ids]
            )
            self._archive.store_all(second.dropped)

        text = render_items(second.selected)
        return RenderResult(
            text=text,
            selected_ids=[i.item_id for i in second.selected],
            context_chars=len(text),
            usage=usage,
            compressed_ids=[i.item_id for i in compressed],
            degraded_ids=degraded,
            archived=self._archive.refs if self._archive is not None else [],
            llm_used=usage.calls > 0,
        )
