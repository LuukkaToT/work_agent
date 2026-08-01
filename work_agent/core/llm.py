from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from work_agent.core.config import get_settings


def get_chat_model(
    *,
    temperature: float | None = None,
    model: str | None = None,
) -> BaseChatModel:
    """全项目唯一入口：禁止在别处直接 new ChatOpenAI。"""
    s = get_settings()
    if not s.llm_api_key:
        raise RuntimeError(
            "未找到 LLM_API_KEY / GEMINI_API_KEY，请检查仓库根目录 .env"
        )

    return ChatOpenAI(
        model=model or s.llm_model,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        temperature=s.llm_temperature if temperature is None else temperature,
        timeout=s.llm_timeout,
        max_retries=s.llm_max_retries,
    )