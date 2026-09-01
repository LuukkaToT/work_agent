"""真实 Streamable HTTP：Bearer、Host、tools/call 与完成后 resume 幂等。"""

from __future__ import annotations

import socket
from contextlib import asynccontextmanager
from dataclasses import replace

import anyio
import uvicorn
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from tests.mcp.test_testing_agent_server import FakeTurnService
from work_agent.core.config import get_settings
from work_agent.mcp.http import build_mcp_http_mount

TOKEN = "streamable-secret"
ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _mcp_http_app(turn_service: FakeTurnService) -> FastAPI:
    settings = replace(
        get_settings(),
        mcp_enabled=True,
        mcp_service_token=TOKEN,
        mcp_allowed_hosts=ALLOWED_HOSTS,
    )
    mount = build_mcp_http_mount(settings, turn_service=turn_service)
    assert mount is not None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mount.server.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan)
    app.mount("/", mount.app, name="mcp")
    return app


async def _wait_started(server: uvicorn.Server) -> None:
    with anyio.fail_after(5):
        while not server.started:
            await anyio.sleep(0.05)


def test_streamable_http_tools_call_and_idempotent_resume():
    fake = FakeTurnService()
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    app = _mcp_http_app(fake)

    async def run() -> None:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = False

        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve)
            await _wait_started(server)
            try:
                http = create_mcp_http_client(
                    headers={"Authorization": f"Bearer {TOKEN}"}
                )
                async with http:
                    async with Client(
                        streamable_http_client(url, http_client=http)
                    ) as client:
                        tools = await client.list_tools()
                        names = {tool.name for tool in tools.tools}
                        assert names == {
                            "testing_agent_turn",
                            "testing_agent_resume",
                            "testing_agent_status",
                        }

                        started = await client.call_tool(
                            "testing_agent_turn",
                            {"message": "跑一下", "user_id": "z00888363"},
                        )
                        assert started.is_error is False
                        assert started.structured_content["status"] == "waiting_input"
                        thread_id = started.structured_content["thread_id"]

                        first = await client.call_tool(
                            "testing_agent_resume",
                            {
                                "thread_id": thread_id,
                                "user_id": "z00888363",
                                "answer": {"indices": [1, 2]},
                            },
                        )
                        replay = await client.call_tool(
                            "testing_agent_resume",
                            {
                                "thread_id": thread_id,
                                "user_id": "z00888363",
                                "answer": "timeout-retry",
                            },
                        )
                        status = await client.call_tool(
                            "testing_agent_status",
                            {"thread_id": thread_id, "user_id": "z00888363"},
                        )
                        assert first.structured_content["reply"] == "已创建"
                        assert replay.structured_content == first.structured_content
                        assert status.structured_content["summary"] == {
                            "status": "created"
                        }
            finally:
                server.should_exit = True

    anyio.run(run)
    assert fake.resume_executions == 1
    assert fake.answers == [{"indices": [1, 2]}]
