"""
LLM token 用量归集。

存在的理由：A/B eval 要求把「上下文压缩自己烧掉的 token」也算进总量。
否则 managed 策略看着又省又快，成本其实藏在摘要调用里，对比就是自欺。
``core/llm.py`` 的 ``invoke_text`` 只返回文本、丢掉了用量，所以需要用量的
调用方必须自己 ``invoke`` 拿 AIMessage，再用这里的函数抽。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TokenUsage:
    """一次或多次 LLM 调用累计的 token 用量。"""

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        """输入 + 输出。"""
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """累加两次用量（供逐轮汇总）。"""
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            calls=self.calls + other.calls,
        )

    def as_dict(self) -> dict[str, int]:
        """落 eval 记录用的扁平字典。"""
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "total": self.total_tokens,
            "calls": self.calls,
        }


def usage_from_message(message: Any) -> TokenUsage:
    """
    从模型返回消息里抽 token 用量。

    ``usage_metadata`` 是 LangChain 的标准字段；部分 OpenAI 兼容端点只给
    ``response_metadata['token_usage']``，两处都试。都没有则返回零值且
    ``calls=1`` —— 统计缺失不该打断诊断，但调用次数仍要记。

    参数:
        message: 模型返回的 AIMessage（或任何带上述属性的对象）。

    返回:
        本次调用的 TokenUsage。
    """
    meta = getattr(message, "usage_metadata", None)
    if isinstance(meta, dict):
        return TokenUsage(
            input_tokens=int(meta.get("input_tokens") or 0),
            output_tokens=int(meta.get("output_tokens") or 0),
            calls=1,
        )
    resp = getattr(message, "response_metadata", None)
    if isinstance(resp, dict):
        raw = resp.get("token_usage") or resp.get("usage")
        if isinstance(raw, dict):
            return TokenUsage(
                input_tokens=int(raw.get("prompt_tokens") or 0),
                output_tokens=int(raw.get("completion_tokens") or 0),
                calls=1,
            )
    return TokenUsage(calls=1)
