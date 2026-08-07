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
    """调用模型并把结果收敛为指定 Pydantic 契约。

    业务节点只依赖该入口，后续真实模型若不兼容原生 Structured Output，可在
    此处集中实现 JSON 提取、校验和有限重试。
    """

    model = get_chat_model(temperature=temperature).with_structured_output(schema)
    result = model.invoke(messages)
    if isinstance(result, schema):
        return result
    return schema.model_validate(result)
