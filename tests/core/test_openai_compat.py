"""Gemini OpenAI 兼容端点：tool_call extra_content 必须能原样回传。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai.chat_models import base as openai_base

from work_agent.core.openai_compat import (
    CompatChatOpenAI,
    _OPENAI_TOOL_CALL_EXTRAS_KEY,
    _SKIP_THOUGHT_SIGNATURE,
    ensure_gemini_thought_signatures,
    is_gemini_openai_compat_url,
)


def test_is_gemini_openai_compat_url():
    assert is_gemini_openai_compat_url(
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    assert not is_gemini_openai_compat_url("https://openrouter.ai/api/v1")
    assert not is_gemini_openai_compat_url("")


def test_tool_call_extras_round_trip():
    raw = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_abc",
                "type": "function",
                "function": {"name": "greet", "arguments": "{}"},
                "extra_content": {"google": {"thought_signature": "SIG=="}},
            }
        ],
    }
    message = openai_base._convert_dict_to_message(raw)
    assert isinstance(message, AIMessage)
    assert message.tool_calls[0]["id"] == "call_abc"
    assert message.additional_kwargs[_OPENAI_TOOL_CALL_EXTRAS_KEY] == {
        "call_abc": {"extra_content": {"google": {"thought_signature": "SIG=="}}}
    }
    out = openai_base._convert_message_to_dict(message)
    assert out["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": "SIG=="}
    }
    assert out["tool_calls"][0]["function"] == {"name": "greet", "arguments": "{}"}


def test_tool_call_without_extras_unchanged():
    message = AIMessage(
        content="",
        tool_calls=[{"name": "f", "args": {"a": 1}, "id": "x", "type": "tool_call"}],
    )
    out = openai_base._convert_message_to_dict(message)
    assert out["tool_calls"] == [
        {
            "type": "function",
            "id": "x",
            "function": {"name": "f", "arguments": '{"a": 1}'},
        }
    ]


def test_gemini_fallback_signature_only_when_missing():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                {
                    "id": "b",
                    "type": "function",
                    "function": {"name": "g", "arguments": "{}"},
                    "extra_content": {"google": {"thought_signature": "KEEP"}},
                },
            ],
        }
    ]
    ensure_gemini_thought_signatures(messages)
    assert messages[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == (
        _SKIP_THOUGHT_SIGNATURE
    )
    assert messages[0]["tool_calls"][1]["extra_content"]["google"]["thought_signature"] == "KEEP"


def test_compat_chat_openai_injects_signature_for_gemini_payload():
    model = CompatChatOpenAI(
        model="gemini-3.6-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key="test-key",
    )
    history = [
        HumanMessage(content="hi"),
        AIMessage(
            content="",
            tool_calls=[{"name": "ping", "args": {}, "id": "call_1", "type": "tool_call"}],
        ),
    ]
    payload = model._get_request_payload(history)
    tool_calls = payload["messages"][1]["tool_calls"]
    assert tool_calls[0]["extra_content"]["google"]["thought_signature"] == (
        _SKIP_THOUGHT_SIGNATURE
    )


def test_compat_chat_openai_does_not_inject_for_other_gateways():
    model = CompatChatOpenAI(
        model="some-model",
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
    )
    history = [
        AIMessage(
            content="",
            tool_calls=[{"name": "ping", "args": {}, "id": "call_1", "type": "tool_call"}],
        )
    ]
    payload = model._get_request_payload(history)
    assert "extra_content" not in payload["messages"][0]["tool_calls"][0]
