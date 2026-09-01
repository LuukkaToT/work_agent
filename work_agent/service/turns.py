"""会话式 Testing Agent 的共享入口服务。"""

from __future__ import annotations

import re
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from work_agent import runtime
from work_agent.core.session_locks import try_acquire_thread_lock

EventFn = Callable[[str], None]
_USER_ID_RE = re.compile(r"^[a-z][0-9]{8}$")


class TurnServiceError(RuntimeError):
    """带稳定错误码的入口层错误。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class TurnResult(BaseModel):
    """REST 与 MCP 共用的精简会话结果。"""

    thread_id: str
    status: Literal["done", "waiting_input"]
    reply: str | None = None
    summary: dict[str, Any] | None = None
    interrupt: list[Any] = Field(default_factory=list)


def normalize_user_id(user_id: str) -> str:
    """把工号规范化为小写，并校验“一位字母 + 8 位数字”。"""
    normalized = (user_id or "").strip().lower()
    if not _USER_ID_RE.fullmatch(normalized):
        raise TurnServiceError("INVALID_USER", "工号必须是一个英文字母加 8 位数字")
    return normalized


def _assert_thread_owner(thread_id: str, user_id: str) -> None:
    if not (thread_id or "").startswith(f"{user_id}-"):
        raise TurnServiceError("THREAD_FORBIDDEN", "无权访问该会话")


def turn_result_from_runtime(result: dict[str, Any]) -> TurnResult:
    """只挑选稳定对外字段，不泄漏 graph state 或 audit。"""
    thread_id = str(result.get("_thread_id") or "")
    interrupts = runtime.interrupt_payloads(result)
    if interrupts:
        return TurnResult(
            thread_id=thread_id,
            status="waiting_input",
            interrupt=interrupts,
        )
    return TurnResult(
        thread_id=thread_id,
        status="done",
        reply=result.get("reply"),
        summary=result.get("summary"),
    )


class TurnService:
    """封装 runtime、会话归属和同 thread 互斥。"""

    def turn(
        self,
        message: str,
        *,
        user_id: str,
        thread_id: str | None = None,
        on_event: EventFn | None = None,
    ) -> TurnResult:
        text = (message or "").strip()
        if not text:
            raise TurnServiceError("INVALID_MESSAGE", "message 不能为空")
        uid = normalize_user_id(user_id)
        supplied_thread = bool((thread_id or "").strip())
        tid = (thread_id or "").strip() or runtime.new_thread_id(uid)
        _assert_thread_owner(tid, uid)

        lock = try_acquire_thread_lock(tid)
        if lock is None:
            raise TurnServiceError("THREAD_BUSY", "该会话正在处理上一条请求")
        try:
            if supplied_thread:
                current = runtime.get_turn_status(tid)
                if current is None:
                    raise TurnServiceError("THREAD_NOT_FOUND", "会话不存在")
                if runtime.interrupt_payloads(current):
                    raise TurnServiceError(
                        "THREAD_PENDING",
                        "会话正在等待回答，请调用 testing_agent_resume",
                    )
            kwargs: dict[str, Any] = {"thread_id": tid, "user_id": uid}
            if on_event is not None:
                kwargs["on_event"] = on_event
            result = runtime.run_turn_step(text, **kwargs)
        finally:
            lock.release()
        return turn_result_from_runtime(result)

    def status(self, *, user_id: str, thread_id: str) -> TurnResult:
        uid = normalize_user_id(user_id)
        tid = (thread_id or "").strip()
        _assert_thread_owner(tid, uid)
        result = runtime.get_turn_status(tid)
        if result is None:
            raise TurnServiceError("THREAD_NOT_FOUND", "会话不存在")
        return turn_result_from_runtime(result)

    def resume(
        self,
        answer: Any,
        *,
        user_id: str,
        thread_id: str,
        on_event: EventFn | None = None,
        return_current_if_completed: bool = False,
    ) -> TurnResult:
        if answer is None:
            raise TurnServiceError("INVALID_ANSWER", "answer 不能为空")
        if isinstance(answer, str) and not answer.strip():
            raise TurnServiceError("INVALID_ANSWER", "answer 不能为空")
        if isinstance(answer, (dict, list)) and not answer:
            raise TurnServiceError("INVALID_ANSWER", "answer 不能为空")

        uid = normalize_user_id(user_id)
        tid = (thread_id or "").strip()
        _assert_thread_owner(tid, uid)
        lock = try_acquire_thread_lock(tid)
        if lock is None:
            raise TurnServiceError("THREAD_BUSY", "该会话正在处理上一条请求")
        try:
            kwargs: dict[str, Any] = {}
            if on_event is not None:
                kwargs["on_event"] = on_event
            result = runtime.resume_step(tid, answer, **kwargs)
            if result is None:
                if not return_current_if_completed:
                    raise TurnServiceError(
                        "THREAD_NOT_PENDING", "该会话当前没有待回答的问题"
                    )
                current = runtime.get_turn_status(tid)
                if current is None:
                    raise TurnServiceError("THREAD_NOT_FOUND", "会话不存在")
                result = current
        finally:
            lock.release()
        return turn_result_from_runtime(result)
