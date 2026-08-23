"""FastAPI 请求 / 响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class TurnRequest(BaseModel):
    """``POST /turns`` 请求体。"""

    message: str = Field(..., min_length=1, description="本轮用户自然语言输入")
    thread_id: str | None = Field(
        default=None, description="会话 id；留空则新建一个（前缀为当前用户工号）"
    )


class ResumeRequest(BaseModel):
    """``POST /turns/{thread_id}/resume`` 请求体。"""

    answer: str = Field(..., min_length=1, description="对上一个 interrupt 的回答")


class TurnResponse(BaseModel):
    """跑一轮 / 续跑一轮的统一响应形状。"""

    thread_id: str
    status: Literal["done", "waiting_input"]
    reply: str | None = Field(default=None, description="status=done 时的最终回复")
    summary: dict[str, Any] | None = Field(
        default=None, description="status=done 时的结构化 summary"
    )
    interrupt: list[Any] = Field(
        default_factory=list,
        description="status=waiting_input 时的原始 interrupt 载荷列表",
    )


class SessionSummary(BaseModel):
    """``GET /sessions`` 里的一条会话摘要。"""

    thread_id: str
    updated_at: str
    preview: str
    pending: bool = False
