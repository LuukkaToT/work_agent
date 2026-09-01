"""MCP Streamable HTTP 的启用配置和服务令牌中间件。"""

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from work_agent.core.config import get_settings
from work_agent.mcp.http import (
    ServiceBearerAuthMiddleware,
    build_mcp_http_mount,
)


async def _ok(_request):
    return JSONResponse({"ok": True})


def test_service_bearer_auth_rejects_missing_and_wrong_token():
    inner = Starlette(routes=[Route("/", _ok)])
    client = TestClient(ServiceBearerAuthMiddleware(inner, "secret-token"))

    missing = client.get("/")
    wrong = client.get("/", headers={"Authorization": "Bearer wrong"})

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert wrong.status_code == 401


def test_service_bearer_auth_accepts_exact_token():
    inner = Starlette(routes=[Route("/", _ok)])
    client = TestClient(ServiceBearerAuthMiddleware(inner, "secret-token"))
    response = client.get(
        "/", headers={"Authorization": "Bearer secret-token"}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_disabled_mcp_builds_no_mount():
    settings = replace(get_settings(), mcp_enabled=False)
    assert build_mcp_http_mount(settings) is None


@pytest.mark.parametrize(
    ("token", "hosts", "message"),
    [
        ("", ("testing.example",), "MCP_SERVICE_TOKEN"),
        ("token", (), "MCP_ALLOWED_HOSTS"),
    ],
)
def test_enabled_mcp_requires_secure_config(token, hosts, message):
    settings = replace(
        get_settings(),
        mcp_enabled=True,
        mcp_service_token=token,
        mcp_allowed_hosts=hosts,
    )
    with pytest.raises(RuntimeError, match=message):
        build_mcp_http_mount(settings)


def test_enabled_mcp_builds_streamable_http_mount():
    settings = replace(
        get_settings(),
        mcp_enabled=True,
        mcp_service_token="token",
        mcp_allowed_hosts=("testing.example",),
    )
    mount = build_mcp_http_mount(settings)
    assert mount is not None
    assert mount.server.name == "testing-agent"


def _gateway_with_mcp(settings, *, turn_service=None) -> FastAPI:
    mount = build_mcp_http_mount(settings, turn_service=turn_service)
    assert mount is not None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mount.server.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan)

    @app.post("/turns")
    def create_turn():
        return {"ok": True}

    app.mount("/", mount.app, name="mcp")
    return app


def test_mounted_mcp_rejects_missing_bearer_and_keeps_rest():
    settings = replace(
        get_settings(),
        mcp_enabled=True,
        mcp_service_token="secret-token",
        mcp_allowed_hosts=("testserver",),
    )
    with TestClient(_gateway_with_mcp(settings)) as client:
        missing = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"

        rest = client.post("/turns")
        assert rest.status_code == 200
        assert rest.json() == {"ok": True}


def test_mounted_mcp_rejects_disallowed_host():
    settings = replace(
        get_settings(),
        mcp_enabled=True,
        mcp_service_token="secret-token",
        mcp_allowed_hosts=("testserver",),
    )
    app = _gateway_with_mcp(settings)
    with TestClient(app, base_url="http://evil.example") as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Authorization": "Bearer secret-token"},
        )
    assert response.status_code == 421
    assert "Invalid Host header" in response.text
