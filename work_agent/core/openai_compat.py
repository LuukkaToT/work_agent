"""
OpenAI 兼容端点上的供应商扩展字段。

Gemini 3 多轮 function calling 要求把 ``thought_signature`` 原样回传，否则下一轮
400。官方 ``ChatOpenAI`` 按 OpenAI 规范丢掉非标准字段；最新 ``langchain-openai``
也没有保留。这里只补这一层，工厂仍然走 ``/chat/completions``，公司网关不受影响。
"""

from __future__ import annotations

from typing import Any, Mapping

from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models import base as openai_base

_OPENAI_TOOL_CALL_EXTRAS_KEY = "__openai_tool_call_extras__"
_RESERVED_TOOL_CALL_KEYS = frozenset({"id", "type", "function", "index"})
_SKIP_THOUGHT_SIGNATURE = "skip_thought_signature_validator"
_GEMINI_OPENAI_HOST = "generativelanguage.googleapis.com"

_installed = False
_orig_dict_to_message = openai_base._convert_dict_to_message
_orig_message_to_dict = openai_base._convert_message_to_dict


def is_gemini_openai_compat_url(base_url: str | None) -> bool:
    """是否指向 Gemini 的 OpenAI 兼容端点。"""
    return _GEMINI_OPENAI_HOST in (base_url or "")


def capture_tool_call_extras(
    raw_tool_call: Mapping[str, Any], *, fallback_key: str | None = None
) -> tuple[str | None, dict[str, Any]]:
    """抽出 tool_call 上 OpenAI 规范之外的字段（例如 extra_content）。"""
    extras = {
        key: value
        for key, value in raw_tool_call.items()
        if key not in _RESERVED_TOOL_CALL_KEYS
    }
    if not extras:
        return None, {}
    return raw_tool_call.get("id") or fallback_key, extras


def ensure_gemini_thought_signatures(messages: list[dict[str, Any]]) -> None:
    """Gemini 3：缺签名的 functionCall 填官方允许的跳过标记，避免直接 400。"""
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            extra = tool_call.get("extra_content")
            google = extra.get("google") if isinstance(extra, dict) else None
            if isinstance(google, dict) and google.get("thought_signature"):
                continue
            google_out = dict(google) if isinstance(google, dict) else {}
            google_out["thought_signature"] = _SKIP_THOUGHT_SIGNATURE
            extra_out = dict(extra) if isinstance(extra, dict) else {}
            extra_out["google"] = google_out
            tool_call["extra_content"] = extra_out


def _patched_dict_to_message(_dict: Mapping[str, Any]) -> BaseMessage:
    message = _orig_dict_to_message(_dict)
    if not isinstance(message, AIMessage):
        return message
    extras: dict[str, dict[str, Any]] = dict(
        message.additional_kwargs.get(_OPENAI_TOOL_CALL_EXTRAS_KEY) or {}
    )
    for raw_tool_call in _dict.get("tool_calls") or []:
        if not isinstance(raw_tool_call, Mapping):
            continue
        key, captured = capture_tool_call_extras(raw_tool_call)
        if key and captured:
            extras[str(key)] = captured
    if extras:
        message.additional_kwargs[_OPENAI_TOOL_CALL_EXTRAS_KEY] = extras
    return message


def _patched_message_to_dict(
    message: BaseMessage,
    api: str = "chat/completions",
) -> dict[str, Any]:
    payload = _orig_message_to_dict(message, api=api)
    if payload.get("role") != "assistant" or not payload.get("tool_calls"):
        return payload
    extras_map: dict[str, dict[str, Any]] = {}
    if isinstance(message, AIMessage):
        extras_map = dict(
            message.additional_kwargs.get(_OPENAI_TOOL_CALL_EXTRAS_KEY) or {}
        )
        for index, tool_call in enumerate(message.tool_calls or []):
            placeholder = f"__index_{index}"
            tool_call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
            if placeholder in extras_map and tool_call_id and tool_call_id not in extras_map:
                extras_map[str(tool_call_id)] = extras_map.pop(placeholder)
    for tool_call in payload.get("tool_calls") or []:
        extra = extras_map.get(str(tool_call.get("id") or ""))
        if extra:
            for key, value in extra.items():
                tool_call.setdefault(key, value)
    return payload


def install_openai_compat_patches() -> None:
    """把转换函数换成会回传 extra_content 的版本；进程内只装一次。"""
    global _installed
    if _installed:
        return
    openai_base._convert_dict_to_message = _patched_dict_to_message
    openai_base._convert_message_to_dict = _patched_message_to_dict
    _installed = True


class CompatChatOpenAI(ChatOpenAI):
    """ChatOpenAI 子类：Gemini 兼容端点在出站消息上补 thought_signature。"""

    def _get_request_payload(self, input_, *, stop=None, **kwargs):  # noqa: ANN001
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        base_url = str(
            getattr(self, "openai_api_base", None) or getattr(self, "base_url", None) or ""
        )
        if is_gemini_openai_compat_url(base_url):
            ensure_gemini_thought_signatures(payload.get("messages") or [])
        return payload


install_openai_compat_patches()
