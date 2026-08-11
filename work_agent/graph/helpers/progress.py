"""可选进度回调（CLI 状态条 / 工具名），用 ContextVar 避免改图签名。"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Callable

ProgressFn = Callable[[str], None]

_progress_hook: ContextVar[ProgressFn | None] = ContextVar(
    "work_agent_progress_hook", default=None
)


def set_progress_hook(fn: ProgressFn | None):
    """
    设置当前上下文的进度回调。

    参数:
        fn: 接收一行进度字符串的回调；None 表示清空。

    返回:
        ContextVar token，交给 reset_progress_hook 恢复。
    """
    return _progress_hook.set(fn)


def reset_progress_hook(token) -> None:
    """
    恢复 set_progress_hook 之前的回调。

    参数:
        token: set_progress_hook 返回的 token。
    """
    _progress_hook.reset(token)


def report_progress(message: str) -> None:
    """
    若有 hook 则上报一行进度（如 node:router / tool:fetch_logs）。

    参数:
        message: 进度事件字符串。
    """
    fn = _progress_hook.get()
    if fn is not None:
        fn(message)
