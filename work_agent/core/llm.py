"""
LLM 唯一工厂。

全项目只允许通过 get_chat_model() 拿模型，方便：
- 学习阶段：base_url 指向 Gemini 的 OpenAI 兼容端点
- 上公司后：只改 .env 三行（BASE_URL / API_KEY / MODEL），代码不动
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from work_agent.core.config import get_settings


def get_chat_model(
    *,
    temperature: float | None = None,
    model: str | None = None,
) -> BaseChatModel:
    s = get_settings()
    if not s.llm_api_key:
        raise RuntimeError(
            "未找到 LLM_API_KEY / GEMINI_API_KEY，请检查仓库根目录 .env"
        )

    # ChatOpenAI 走 /chat/completions；Gemini 兼容端点支持这个，不支持 /responses
    return ChatOpenAI(
        model=model or s.llm_model,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        temperature=s.llm_temperature if temperature is None else temperature,
        timeout=s.llm_timeout,
        max_retries=s.llm_max_retries,
    )
