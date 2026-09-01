"""MCP Streamable HTTP 挂载、Host 防护与服务令牌认证。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from work_agent.core.config import Settings
from work_agent.mcp.server import create_testing_agent_mcp
from work_agent.service.turns import TurnService


class ServiceBearerAuthMiddleware:
    """只保护 MCP 子应用的静态 Bearer 服务令牌。"""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        authorization = Headers(scope=scope).get("authorization", "")
        scheme, separator, supplied = authorization.partition(" ")
        valid = (
            separator == " "
            and scheme.casefold() == "bearer"
            and bool(supplied)
            and secrets.compare_digest(supplied, self.token)
        )
        if not valid:
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


@dataclass(frozen=True)
class McpHttpMount:
    server: MCPServer
    app: ASGIApp


def build_mcp_http_mount(
    settings: Settings,
    *,
    turn_service: TurnService | None = None,
) -> McpHttpMount | None:
    """按配置构造 `/mcp` 子应用；关闭时返回 None。"""
    if not settings.mcp_enabled:
        return None
    if not settings.mcp_service_token:
        raise RuntimeError("MCP_ENABLED=true 时必须配置 MCP_SERVICE_TOKEN")
    if not settings.mcp_allowed_hosts:
        raise RuntimeError("MCP_ENABLED=true 时必须配置 MCP_ALLOWED_HOSTS")

    server = create_testing_agent_mcp(turn_service)
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.mcp_allowed_hosts),
        allowed_origins=list(settings.mcp_allowed_origins),
    )
    mcp_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=False,
        json_response=False,
        transport_security=transport_security,
    )
    protected = ServiceBearerAuthMiddleware(mcp_app, settings.mcp_service_token)
    return McpHttpMount(server=server, app=protected)
