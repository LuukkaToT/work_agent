"""
CLI 侧身份来源：进程级拿工号，与 HTTP 网关的请求级鉴权形状不同。

HTTP 侧（``work_agent.api.identity.HeaderIdentityProvider``）每次请求从
``X-User-Id`` 头注入；CLI 是单进程单用户，没有请求头，读环境变量即可。
两者都产出同一个业务身份——工号字符串（如 ``z00888363``），但调用形状
不能硬套成同一个 Protocol（请求级 vs 进程级），所以这里单独定义
``IdentityProvider``，专给 CLI / 脚本用。

``DEFAULT_USER_ID`` 同时也是 ``core/ledger.py`` 回填历史空 ``user_id`` 记录时
用的目标值——两处必须是同一个常量：以前没人配 ``WORK_AGENT_USER_ID`` 时留下的
历史数据，语义上就该归到"没配工号时大家默认用的这个身份"，不能各自定义一份。
"""

from __future__ import annotations

import os
from typing import Protocol

DEFAULT_USER_ID = "local-dev"


class IdentityProvider(Protocol):
    """进程级身份来源：返回当前用户的工号字符串。"""

    def get_user_id(self) -> str: ...


class EnvIdentityProvider:
    """
    读 ``WORK_AGENT_USER_ID`` 环境变量；未配置或空串时退回固定默认值 ``local-dev``。

    本地开发可以不配（用默认值），上线前在部署环境里把真实工号写进这个变量。
    """

    env_var = "WORK_AGENT_USER_ID"

    def get_user_id(self) -> str:
        """
        返回:
            环境变量里的工号；未配置时返回 ``local-dev``。
        """
        value = (os.getenv(self.env_var) or "").strip()
        return value or DEFAULT_USER_ID


class StaticIdentityProvider:
    """固定返回给定 ``user_id``；测试 / 脚本用，不读环境。"""

    def __init__(self, user_id: str) -> None:
        """
        参数:
            user_id: 固定返回的工号；空串不允许（身份缺失应显式用 Env 回退路径）。
        """
        if not user_id or not user_id.strip():
            raise ValueError("StaticIdentityProvider 的 user_id 不能为空")
        self._user_id = user_id.strip()

    def get_user_id(self) -> str:
        """返回构造时给定的工号。"""
        return self._user_id
