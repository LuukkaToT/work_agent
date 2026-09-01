"""Testing Agent MCP tools 的发现、结构化结果与 progress。"""

from __future__ import annotations

from typing import Any

import anyio
from mcp import Client

from work_agent.mcp.server import create_testing_agent_mcp
from work_agent.service.turns import TurnResult, TurnServiceError


class FakeTurnService:
    def __init__(self) -> None:
        self.answers: list[Any] = []
        self.resume_executions = 0
        self._done: dict[str, TurnResult] = {}

    def turn(self, message, *, user_id, thread_id=None, on_event=None):
        if on_event:
            on_event("node:intake")
            on_event("status:waiting_input")
        return TurnResult(
            thread_id=thread_id or f"{user_id}-abc123",
            status="waiting_input",
            interrupt=[{"type": "confirm_exec", "message": "确认执行？"}],
        )

    def resume(
        self,
        answer,
        *,
        user_id,
        thread_id,
        on_event=None,
        return_current_if_completed=False,
    ):
        if thread_id in self._done:
            if return_current_if_completed:
                return self._done[thread_id]
            raise TurnServiceError("THREAD_NOT_PENDING", "该会话当前没有待回答的问题")
        self.resume_executions += 1
        self.answers.append(answer)
        if on_event:
            on_event("node:create_pipelines")
        result = TurnResult(
            thread_id=thread_id,
            status="done",
            reply="已创建",
            summary={"status": "created"},
        )
        self._done[thread_id] = result
        return result

    def status(self, *, user_id, thread_id):
        return TurnResult(
            thread_id=thread_id,
            status="done",
            reply="已创建",
            summary={"status": "created"},
        )


def test_tools_and_structured_hitl_roundtrip():
    fake = FakeTurnService()
    server = create_testing_agent_mcp(fake)
    progress_messages: list[str] = []

    async def run():
        async def on_progress(progress, total, message):
            progress_messages.append(message or "")

        async with Client(server) as client:
            tools = await client.list_tools()
            by_name = {tool.name: tool for tool in tools.tools}
            assert set(by_name) == {
                "testing_agent_turn",
                "testing_agent_resume",
                "testing_agent_status",
            }
            assert by_name["testing_agent_status"].annotations.read_only_hint is True
            assert by_name["testing_agent_turn"].annotations.idempotent_hint is False
            assert set(by_name["testing_agent_turn"].output_schema["properties"]) == {
                "thread_id",
                "status",
                "reply",
                "summary",
                "interrupt",
            }

            started = await client.call_tool(
                "testing_agent_turn",
                {"message": "跑一下", "user_id": "z00888363"},
                progress_callback=on_progress,
            )
            assert started.is_error is False
            assert started.structured_content["status"] == "waiting_input"
            thread_id = started.structured_content["thread_id"]

            resumed = await client.call_tool(
                "testing_agent_resume",
                {
                    "thread_id": thread_id,
                    "user_id": "z00888363",
                    "answer": {"indices": [1, 2]},
                },
                progress_callback=on_progress,
            )
            assert resumed.structured_content["reply"] == "已创建"

            status = await client.call_tool(
                "testing_agent_status",
                {"thread_id": thread_id, "user_id": "z00888363"},
            )
            assert status.structured_content["summary"] == {"status": "created"}

    anyio.run(run)
    assert fake.answers == [{"indices": [1, 2]}]
    assert fake.resume_executions == 1
    assert progress_messages == [
        "node:intake",
        "status:waiting_input",
        "node:create_pipelines",
    ]


def test_resume_after_completion_is_idempotent():
    fake = FakeTurnService()

    async def run():
        async with Client(create_testing_agent_mcp(fake)) as client:
            started = await client.call_tool(
                "testing_agent_turn",
                {"message": "跑一下", "user_id": "z00888363"},
            )
            thread_id = started.structured_content["thread_id"]
            first = await client.call_tool(
                "testing_agent_resume",
                {
                    "thread_id": thread_id,
                    "user_id": "z00888363",
                    "answer": "yes",
                },
            )
            replay = await client.call_tool(
                "testing_agent_resume",
                {
                    "thread_id": thread_id,
                    "user_id": "z00888363",
                    "answer": "yes",
                },
            )
            assert first.structured_content["status"] == "done"
            assert replay.structured_content == first.structured_content

    anyio.run(run)
    assert fake.resume_executions == 1
    assert fake.answers == ["yes"]


def test_service_error_is_returned_as_mcp_tool_error():
    class MissingService(FakeTurnService):
        def status(self, *, user_id, thread_id):
            raise TurnServiceError("THREAD_NOT_FOUND", "会话不存在")

    async def run():
        async with Client(create_testing_agent_mcp(MissingService())) as client:
            result = await client.call_tool(
                "testing_agent_status",
                {"thread_id": "z00888363-none", "user_id": "z00888363"},
            )
            assert result.is_error is True
            assert "THREAD_NOT_FOUND" in result.content[0].text

    anyio.run(run)
