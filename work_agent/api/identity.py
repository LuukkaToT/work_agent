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

# 工号形如 z00888363：字母开头，后面 5~16 位字母数字。故意写得宽松一点，
# 只是为了挡掉明显的空值/乱填，不是真正的工号规则校验。
_GONGHAO_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{4,15}$")


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
        if not x_user_id or not _GONGHAO_RE.match(x_user_id):
            raise HTTPException(
                status_code=401,
                detail="缺少或非法的 X-User-Id 请求头（mock 鉴权，示例：z00888363）",
            )
        return x_user_id


# 单例：路由层统一用 Depends(get_current_user_id)。
get_current_user_id = HeaderIdentityProvider()
