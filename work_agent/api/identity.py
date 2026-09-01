"""
Mock 鉴权：从请求头读工号，不做真实鉴权。

公司侧鉴权接入前，先用这一层跑通「网关按用户隔离会话/台账」的完整流程；
接入真实鉴权时，只需要换掉 ``HeaderIdentityProvider`` 的内部实现（比如改成解析
公司 SSO 的 token/session），路由层的 ``Depends(get_current_user_id)`` 调用形状
不用变——这也是阶段3 ``identity.py`` 要做的事，这里先占住这个位置。
"""

from __future__ import annotations

import re

from fastapi import Header, HTTPException

# 与 users 表、REST/MCP TurnService 保持同一规则。
_GONGHAO_RE = re.compile(r"^[a-z][0-9]{8}$")


class HeaderIdentityProvider:
    """从 ``X-User-Id`` 请求头取工号，校验格式；不存在或格式不对直接 401。"""

    header_name = "x-user-id"

    def __call__(self, x_user_id: str | None = Header(default=None)) -> str:
        """
        FastAPI 依赖入口。

        参数:
            x_user_id: ``X-User-Id`` 请求头（FastAPI 自动按 header_name 注入）。

        返回:
            校验通过的工号字符串。

        异常:
            HTTPException(401): 缺失或格式不对。
        """
        normalized = (x_user_id or "").strip().lower()
        if not _GONGHAO_RE.fullmatch(normalized):
            raise HTTPException(
                status_code=401,
                detail="缺少或非法的 X-User-Id 请求头（mock 鉴权，示例：z00888363）",
            )
        return normalized


# 单例：路由层统一用 Depends(get_current_user_id)。
get_current_user_id = HeaderIdentityProvider()
