"""把整个 Testing Agent 暴露为三个会话式 MCP tools。"""

from __future__ import annotations

import logging
import time
from typing import Any

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from work_agent.core.observability import get_request_id, log_value
from work_agent.service.turns import TurnResult, TurnService, TurnServiceError

logger = logging.getLogger(__name__)


def _progress_callback(ctx: Context):
    progress = 0

    def emit(event: str) -> None:
        nonlocal progress
        progress += 1
        try:
            # runtime 在 worker thread 中执行；把协程安全送回 MCP 事件循环。
            anyio.from_thread.run(
                ctx.report_progress, float(progress), None, str(event)
            )
        except Exception:  # noqa: BLE001
            # progress 是旁路能力，客户端不支持时不能影响主任务。
            return

    return emit


async def _call_service(
    fn,
    *,
    tool_name: str,
    user_id: str,
    thread_id: str | None,
):
    started = time.monotonic()
    try:
        result = await anyio.to_thread.run_sync(fn)
    except TurnServiceError as exc:
        logger.warning(
            "mcp_tool_failed tool=%s user_id=%s thread_id=%s code=%s duration_ms=%d request_id=%s",
            tool_name,
            log_value(user_id),
            log_value(thread_id),
            exc.code,
            int((time.monotonic() - started) * 1000),
            log_value(get_request_id()),
        )
        raise ToolError(str(exc)) from exc
    except Exception:
        logger.exception(
            "mcp_tool_failed tool=%s user_id=%s thread_id=%s code=INTERNAL duration_ms=%d request_id=%s",
            tool_name,
            log_value(user_id),
            log_value(thread_id),
            int((time.monotonic() - started) * 1000),
            log_value(get_request_id()),
        )
        raise
    logger.info(
        "mcp_tool_complete tool=%s user_id=%s thread_id=%s status=%s duration_ms=%d request_id=%s",
        tool_name,
        log_value(user_id),
        log_value(result.thread_id),
        result.status,
        int((time.monotonic() - started) * 1000),
        log_value(get_request_id()),
    )
    return result


def create_testing_agent_mcp(
    turn_service: TurnService | None = None,
) -> MCPServer:
    """构造可供 ASGI 挂载或内存 Client 测试的 MCP server。"""
    service = turn_service or TurnService()
    server = MCPServer(
        name="testing-agent",
        title="Testing Agent",
        description="测试分析、流水线执行/查询与失败诊断的统一会话入口",
        instructions=(
            "先调用 testing_agent_turn。一次 turn 只做一件事："
            "复合任务（例如先创建流水线再分析日志）拆成多次 turn，"
            "后续轮带上同一 thread_id，或在消息里写明 pipeline_id。"
            "返回 waiting_input 时，把 interrupt 展示给用户，再调用 testing_agent_resume；"
            "超时后只调用 testing_agent_status，不要自动重放 turn 或 resume。"
            "内部 Router、Workflow、Diagnose 不是独立 MCP tools。"
        ),
        version="0.1.0",
    )

    @server.tool(
        name="testing_agent_turn",
        title="发起或继续 Testing Agent 会话",
        description=(
            "提交一条用户消息。不传 thread_id 时创建会话；传入已完成会话时继续对话。"
            "若会话正在等待人工输入，改用 testing_agent_resume。"
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
        structured_output=True,
    )
    async def testing_agent_turn(
        message: str,
        user_id: str,
        ctx: Context,
        thread_id: str | None = None,
    ) -> TurnResult:
        progress = _progress_callback(ctx)
        return await _call_service(
            lambda: service.turn(
                message,
                user_id=user_id,
                thread_id=thread_id,
                on_event=progress,
            ),
            tool_name="testing_agent_turn",
            user_id=user_id,
            thread_id=thread_id,
        )

    @server.tool(
        name="testing_agent_resume",
        title="回答 Testing Agent 的待办问题",
        description=(
            "向 waiting_input 会话提交文本、结构化单选或序号多选答案。"
            "若上次调用其实已完成，则幂等返回当前最终状态。"
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
        structured_output=True,
    )
    async def testing_agent_resume(
        thread_id: str,
        user_id: str,
        answer: str | dict[str, Any] | list[int],
        ctx: Context,
    ) -> TurnResult:
        progress = _progress_callback(ctx)
        return await _call_service(
            lambda: service.resume(
                answer,
                user_id=user_id,
                thread_id=thread_id,
                on_event=progress,
                return_current_if_completed=True,
            ),
            tool_name="testing_agent_resume",
            user_id=user_id,
            thread_id=thread_id,
        )

    @server.tool(
        name="testing_agent_status",
        title="查询 Testing Agent 会话状态",
        description="只读查询会话当前结果或待回答的 HITL 载荷。",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    async def testing_agent_status(
        thread_id: str,
        user_id: str,
    ) -> TurnResult:
        return await _call_service(
            lambda: service.status(user_id=user_id, thread_id=thread_id),
            tool_name="testing_agent_status",
            user_id=user_id,
            thread_id=thread_id,
        )

    return server
