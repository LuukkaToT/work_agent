"""
上下文压缩：唯一会调 LLM 的一段。

刻意后置：为省 3000 token 去花 2000 token 调摘要，收益很薄。所以只有
「确定性选择装不下」时才触发，常见路径是零 LLM 开销 —— 与
``nodes/memory.py`` 阈值以下直通的既有做法一致。

摘要自身的 token 会记进 ``CompressionOutcome.usage``，由调用方并入总量。
不这么做的话 managed 策略在 A/B 里会显得又省又快，成本其实藏在这儿。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from work_agent.core.llm import get_fast_model
from work_agent.core.usage import TokenUsage, usage_from_message
from work_agent.graph.helpers.context_budget import compress_observation
from work_agent.graph.helpers.context_selector import ContextItem

_SYSTEM = (
    "把下面这段诊断证据压成不超过 {limit} 字的中文摘要。"
    "必须保留报错关键词原文（如 KeyError、timeout、版本号、用例名、IP）。"
    "不要补充推测，不要下结论，只做压缩。"
)


@dataclass(frozen=True)
class CompressionOutcome:
    """一次压缩的结果。"""

    items: list[ContextItem]  # 压缩后的 item（item_id 保持不变）
    usage: TokenUsage  # 摘要自身的 token 开销
    llm_calls: int  # 实际发生的 LLM 调用次数
    degraded_ids: list[str]  # LLM 失败、降级为确定性截断的 item_id


class ContextCompressor:
    """把过长的上下文块压成摘要；LLM 不可用时降级为确定性截断。"""

    def __init__(
        self,
        model_factory: Callable[[], BaseChatModel] | None = None,
    ) -> None:
        """
        参数:
            model_factory: 返回 chat model 的工厂；None 用 ``get_fast_model``
                （压缩是纯抽取任务，不需要强模型）。单测注入 fake。
        """
        self._model_factory = model_factory or (lambda: get_fast_model(temperature=0))
        self._model: BaseChatModel | None = None

    def compress(
        self,
        items: list[ContextItem],
        *,
        max_chars_each: int,
        suffix: str = "",
    ) -> CompressionOutcome:
        """
        逐块压缩到 ``max_chars_each`` 以内。

        已经够短的块原样返回且不调 LLM —— 这是「摘要后置」的关键，
        调用方不必自己判断哪些值得压。

        参数:
            items: 待压缩的块。
            max_chars_each: 每块压缩后的字符上限。
            suffix: 追加到压缩结果末尾的文本（一般是 archive 引用说明）。

        返回:
            CompressionOutcome。
        """
        out: list[ContextItem] = []
        usage = TokenUsage()
        calls = 0
        degraded: list[str] = []

        for item in items:
            if len(item.text) <= max_chars_each:
                # 已经够短就不烧 token，即便要挂 archive 引用也只是拼个后缀。
                out.append(replace(item, text=f"{item.text}\n{suffix}") if suffix else item)
                continue
            if item.kind == "evidence":
                # 日志/状态证据只做原文摘录，避免摘要模型丢掉恢复行或改写时间关系。
                text = plain_excerpt(item.text, max_chars_each)
                if suffix:
                    text = f"{text}\n{suffix}"
                out.append(replace(item, text=text))
                continue

            text, item_usage, ok = self._summarize(item.text, limit=max_chars_each)
            usage = usage + item_usage
            if item_usage.calls:
                calls += item_usage.calls
            if not ok:
                degraded.append(item.item_id)
            if suffix:
                text = f"{text}\n{suffix}"
            out.append(replace(item, text=text))

        return CompressionOutcome(
            items=out, usage=usage, llm_calls=calls, degraded_ids=degraded
        )

    def _summarize(self, text: str, *, limit: int) -> tuple[str, TokenUsage, bool]:
        """调 LLM 压缩；任何异常都降级为 ``compress_observation``。"""
        try:
            model = self._ensure_model()
            resp = model.invoke(
                [
                    SystemMessage(content=_SYSTEM.format(limit=limit)),
                    HumanMessage(content=text),
                ]
            )
            usage = usage_from_message(resp)
            summary = _as_text(getattr(resp, "content", ""))
            if not summary:
                return compress_observation(text, max_chars=limit), usage, False
            if len(summary) > limit:
                summary = compress_observation(summary, max_chars=limit)
            return summary, usage, True
        except Exception:  # noqa: BLE001
            # 摘要失败不能拖垮诊断：退回确定性截断，并记为降级。
            return compress_observation(text, max_chars=limit), TokenUsage(), False

    def compress_batch(
        self, items: list[ContextItem], *, max_chars_each: int,
    ) -> CompressionOutcome:
        """抽取阶段的必保留块一次摘要；格式错误不重试，不丢已发生的用量。

        JSON 的键必须与待压块 ID 完全一致，避免把某块摘要挂到另一份原文。
        普通块是否应被压缩由调用方决定；原 compress 接口行为保持不变。
        """
        if max_chars_each < 32:
            raise ValueError("批量摘要每块至少需要 32 字符")
        prepared: list[ContextItem] = []
        for item in items:
            if item.kind == "evidence" and len(item.text) > max_chars_each:
                prepared.append(replace(item, text=plain_excerpt(item.text, max_chars_each)))
            else:
                prepared.append(item)
        targets = [
            item for item in prepared
            if item.kind != "evidence" and len(item.text) > max_chars_each
        ]
        if not targets:
            return CompressionOutcome(prepared, TokenUsage(), 0, [])
        usage = TokenUsage()
        summaries: dict[str, str] = {}
        degraded: list[str] = []
        try:
            model = self._ensure_model()
            usage = TokenUsage(calls=1)
            response = model.invoke([
                SystemMessage(content=(
                    "逐块压缩诊断结论，保留故障类别、根因组件、证据、已排除假设及未解决矛盾。"
                    "保留原文标识、参数、否定与时间关系，不新增推测，不按报错关键词过滤正常或恢复证据。"
                    f"每块不超过 {max_chars_each} 字符。只返回 JSON 对象，键为输入块 ID，值为摘要字符串；"
                    "必须保留全部 ID，不合并块，不输出 Markdown 围栏。"
                )),
                HumanMessage(content=json.dumps(
                    {item.item_id: item.text for item in targets}, ensure_ascii=False,
                )),
            ])
            usage = usage_from_message(response)
            parsed = json.loads(_as_text(response.content))
            if (
                not isinstance(parsed, dict)
                or set(parsed) != {item.item_id for item in targets}
                or any(not isinstance(value, str) or not value.strip() for value in parsed.values())
            ):
                raise ValueError("批量摘要块 ID 或正文无效")
            summaries = parsed
        except Exception:  # noqa: BLE001 - 一次失败即原文摘录降级
            degraded = [item.item_id for item in targets]
        target_ids = {item.item_id for item in targets}
        return CompressionOutcome(
            items=[
                replace(
                    item,
                    text=plain_excerpt(summaries.get(item.item_id, item.text), max_chars_each),
                )
                if item.item_id in target_ids
                else item
                for item in prepared
            ],
            usage=usage, llm_calls=usage.calls, degraded_ids=degraded,
        )

    def _ensure_model(self) -> BaseChatModel:
        """惰性构造并复用 model（避免每块都新建客户端）。"""
        if self._model is None:
            self._model = self._model_factory()
        return self._model


class DeterministicContextCompressor(ContextCompressor):
    """Prompt projection with no hidden model calls inside an online node."""

    def _summarize(self, text: str, *, limit: int) -> tuple[str, TokenUsage, bool]:
        return compress_observation(text, max_chars=limit), TokenUsage(), True

    def compress_batch(
        self, items: list[ContextItem], *, max_chars_each: int,
    ) -> CompressionOutcome:
        return CompressionOutcome(
            [replace(item, text=plain_excerpt(item.text, max_chars_each)) for item in items],
            TokenUsage(), 0, [],
        )


def plain_excerpt(text: str, limit: int) -> str:
    """有界头尾原文摘录；不将否定、恢复或排除理由过滤掉。"""
    if len(text) <= limit:
        return text
    marker = "\n[原文中段省略]\n"
    if limit <= len(marker):
        return marker[:max(0, limit)]
    room = limit - len(marker)
    head = (room + 1) // 2
    tail = room - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _as_text(content: object) -> str:
    """把 content（str 或分段 list）归一成纯文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "".join(parts).strip()
    return str(content).strip()
