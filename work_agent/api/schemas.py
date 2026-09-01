"""FastAPI 请求 / 响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from work_agent.core.user_config import VERSION_SPACES


class TurnRequest(BaseModel):
    """``POST /turns`` 请求体。"""

    message: str = Field(..., min_length=1, description="本轮用户自然语言输入")
    thread_id: str | None = Field(
        default=None, description="会话 id；留空则新建一个（前缀为当前用户工号）"
    )


class ResumeRequest(BaseModel):
    """``POST /turns/{thread_id}/resume`` 请求体。"""

    answer: str | dict[str, Any] | list[int] = Field(
        ..., description="对上一个 interrupt 的文本、单选或多选回答"
    )

    @field_validator("answer")
    @classmethod
    def answer_must_not_be_empty(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("answer 不能为空")
        if isinstance(value, (dict, list)) and not value:
            raise ValueError("answer 不能为空")
        return value


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


class UserConfigView(BaseModel):
    """``GET/PATCH /users/me/config`` 响应：未设置过的字段为 null。"""

    debug_mode: bool | None = None
    version_space: str | None = None


class UserConfigPatch(BaseModel):
    """``PATCH /users/me/config`` 请求体：只覆盖传入的字段。"""

    debug_mode: bool | None = None
    version_space: str | None = None

    @field_validator("version_space", mode="before")
    @classmethod
    def normalize_version_space(cls, value: object) -> object:
        """空串当清空；非空则转大写，非法值交给后续校验报 422。"""
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text.upper()

    @field_validator("version_space")
    @classmethod
    def version_space_must_be_allowed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in VERSION_SPACES:
            allowed = ", ".join(sorted(VERSION_SPACES))
            raise ValueError(f"必须是 {allowed} 之一")
        return value
