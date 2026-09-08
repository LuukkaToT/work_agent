"""调查截止时间：协作取消，不杀线程。

``future.result(timeout=...)`` 只让调用方停止等待。后台 LLM / 工具可能还在跑，
并继续写 Transcript。本模块在每次模型或工具调用前检查截止，父线程超时先
``cancel()``，禁止再开新调用、禁止再写成功结果。

已在飞行中的 HTTP 无法从另一线程掐断；单次请求 timeout 取
``min(llm_timeout, remaining)`` 作为上界。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable


class DeadlineExceeded(RuntimeError):
    """截止已到或调查已被取消，当前 attempt 必须停转。"""


class Deadline:
    """到期时间 + 取消事件。check 失败必须上抛，不能当成普通工具错误。"""

    def __init__(self, timeout_seconds: float, *, clock: Callable[[], float] = time.perf_counter) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正数")
        self._clock = clock
        self._expires_at = clock() + float(timeout_seconds)
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """父线程超时或租约被接管时调用；之后 check 一律失败。"""
        self._cancelled.set()

    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def remaining(self) -> float:
        if self._cancelled.is_set():
            return 0.0
        return max(0.0, self._expires_at - self._clock())

    def expired(self) -> bool:
        return self._cancelled.is_set() or self.remaining() <= 0.0

    def check(self) -> None:
        if self.expired():
            raise DeadlineExceeded("调查已到截止时间或已被取消")


def invoke_with_deadline(model: Any, messages: Any, deadline: Deadline | None) -> Any:
    """调用前检查截止；尽量把本次 HTTP timeout 收成 min(llm_timeout, remaining)。"""
    if deadline is None:
        return model.invoke(messages)
    deadline.check()
    remaining = max(0.05, deadline.remaining())
    try:
        from work_agent.core.config import get_settings

        remaining = min(remaining, float(get_settings().llm_timeout))
    except Exception:  # noqa: BLE001
        pass
    invoke = model.invoke
    try:
        return invoke(messages, timeout=remaining)
    except TypeError:
        return invoke(messages)
