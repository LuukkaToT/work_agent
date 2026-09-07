"""
上下文压缩：唯一会调 LLM 的一段。

刻意后置：为省 3000 token 去花 2000 token 调摘要，收益很薄。所以只有
「确定性选择装不下」时才触发，常见路径是零 LLM 开销 —— 与
``nodes/memory.py`` 阈值以下直通的既有做法一致。

摘要自身的 token 会记进 ``CompressionOutcome.usage``，由调用方并入总量。
不这么做的话 managed 策略在 A/B 里会显得又省又快，成本其实藏在这儿。
"""

from __future__ import annotations

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

    def _ensure_model(self) -> BaseChatModel:
        """惰性构造并复用 model（避免每块都新建客户端）。"""
        if self._model is None:
            self._model = self._model_factory()
        return self._model


class DeterministicContextCompressor(ContextCompressor):
    """Prompt projection with no hidden model calls inside an online node."""

    def _summarize(self, text: str, *, limit: int) -> tuple[str, TokenUsage, bool]:
        return compress_observation(text, max_chars=limit), TokenUsage(), True


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
