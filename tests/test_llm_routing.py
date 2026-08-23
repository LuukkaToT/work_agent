"""快/慢模型路由：get_fast_model / get_reasoning_model 按 Settings 选型号。"""

from __future__ import annotations

from dataclasses import replace

import work_agent.core.llm as llm_mod
from work_agent.core.config import get_settings
from work_agent.core.llm import get_fast_model, get_reasoning_model


def _settings_with(**overrides):
    base = get_settings()
    return replace(base, **overrides)


def test_get_fast_model_uses_llm_fast_model(monkeypatch):
    monkeypatch.setattr(
        llm_mod,
        "get_settings",
        lambda: _settings_with(
            llm_api_key="test-key", llm_fast_model="fast-x", llm_reasoning_model="reason-y"
        ),
    )
    model = get_fast_model()
    assert model.model_name == "fast-x"


def test_get_reasoning_model_uses_llm_reasoning_model(monkeypatch):
    monkeypatch.setattr(
        llm_mod,
        "get_settings",
        lambda: _settings_with(
            llm_api_key="test-key", llm_fast_model="fast-x", llm_reasoning_model="reason-y"
        ),
    )
    model = get_reasoning_model()
    assert model.model_name == "reason-y"


def test_fast_and_reasoning_default_to_llm_model_when_unset(monkeypatch):
    """未配 LLM_FAST_MODEL/LLM_REASONING_MODEL 时，两者都退回 llm_model。"""
    monkeypatch.setattr(
        llm_mod,
        "get_settings",
        lambda: _settings_with(
            llm_api_key="test-key",
            llm_model="base-model",
            llm_fast_model="base-model",
            llm_reasoning_model="base-model",
        ),
    )
    assert get_fast_model().model_name == "base-model"
    assert get_reasoning_model().model_name == "base-model"


def test_get_settings_env_fallback_to_llm_model(monkeypatch):
    """config.py 层：未设两个新环境变量时，get_settings() 里两者都等于 llm_model。"""
    get_settings.cache_clear()
    monkeypatch.delenv("LLM_FAST_MODEL", raising=False)
    monkeypatch.delenv("LLM_REASONING_MODEL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "gemini-test")
    try:
        settings = get_settings()
        assert settings.llm_fast_model == "gemini-test"
        assert settings.llm_reasoning_model == "gemini-test"
    finally:
        get_settings.cache_clear()


def test_get_settings_reads_fast_and_reasoning_env_vars(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("LLM_MODEL", "gemini-test")
    monkeypatch.setenv("LLM_FAST_MODEL", "gemini-test-fast")
    monkeypatch.setenv("LLM_REASONING_MODEL", "gemini-test-reasoning")
    try:
        settings = get_settings()
        assert settings.llm_fast_model == "gemini-test-fast"
        assert settings.llm_reasoning_model == "gemini-test-reasoning"
    finally:
        get_settings.cache_clear()
        monkeypatch.delenv("LLM_FAST_MODEL", raising=False)
        monkeypatch.delenv("LLM_REASONING_MODEL", raising=False)
