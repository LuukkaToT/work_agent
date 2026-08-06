"""测试分析模块的结构化模型调用入口。"""

from __future__ import annotations

from typing import TypeVar

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from work_agent.core.llm import get_chat_model

T = TypeVar("T", bound=BaseModel)


def invoke_structured(
    schema: type[T],
    messages: list[BaseMessage],
    *,
    temperature: float = 0.1,
) -> T:
    model = get_chat_model(temperature=temperature).with_structured_output(schema)
    result = model.invoke(messages)
    if isinstance(result, schema):
        return result
    return schema.model_validate(result)
