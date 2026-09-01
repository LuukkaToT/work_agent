"""
FastAPI 网关：把 runtime 的两阶段 HITL 包成 HTTP 接口。

- ``POST /turns``：跑一轮；没有 interrupt 就直接给最终结果，遇到 interrupt 就把
  载荷原样带回去，不在服务端阻塞等答案。
- ``GET /turns/{thread_id}``：纯查询当前状态，不触发执行。
- ``POST /turns/{thread_id}/resume``：客户端拿到 interrupt 后，把答案带过来续跑一步。
- ``GET /sessions``：列出当前用户名下的会话。
- ``GET/PATCH /users/me/config``：读写个人偏好。
- ``GET /healthz`` / ``GET /readyz``：探活；不走用户鉴权。
- ``GET /metrics``：Prometheus 文本；不走用户鉴权。

鉴权是 mock 的（见 ``work_agent.api.identity``）。
同一个 thread_id 同一时刻只允许一个请求在跑：有 POSTGRES_DSN 时用
session 级 Postgres advisory lock，否则退回进程内锁；抢不到直接 409。

本地起服务：``uvicorn work_agent.api.app:app --reload``。
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from work_agent import runtime
from work_agent.api.identity import get_current_user_id
from work_agent.api.schemas import (
    ResumeRequest,
    SessionSummary,
    TurnRequest,
    TurnResponse,
    UserConfigPatch,
    UserConfigView,
)
from work_agent.core.config import get_settings
from work_agent.core.db_init import verify_schema_and_seed
from work_agent.core.health import postgres_ready
from work_agent.core.metrics import http_unhandled_total, render_latest
from work_agent.core.observability import (
    configure_logging,
    emit_event,
    reset_request_id,
    set_request_id,
)
from work_agent.core.sessions import list_sessions
from work_agent.core.user_config import (
    get_debug_mode,
    get_version_space,
    set_debug_mode,
    set_version_space,
)
from work_agent.mcp.http import build_mcp_http_mount
from work_agent.service.turns import TurnResult, TurnService, TurnServiceError

_QUIET_PATHS = {"/healthz", "/readyz", "/metrics"}
_turn_service = TurnService()
_mcp_mount = build_mcp_http_mount(get_settings(), turn_service=_turn_service)
logger = logging.getLogger("work_agent.api")


@asynccontextmanager
async def _lifespan(_: FastAPI):
    settings = get_settings()
    configure_logging(
        json_logs=settings.log_format != "text",
        level=settings.log_level,
    )
    verify_schema_and_seed()
    if _mcp_mount is None:
        yield
        return
    async with _mcp_mount.server.session_manager.run():
        yield


app = FastAPI(title="work_agent gateway", version="0.2.0", lifespan=_lifespan)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    """绑定 request_id；未捕获异常打栈并返回 500，不回异常原文。"""
    rid = (request.headers.get("x-request-id") or "").strip() or uuid.uuid4().hex
    token = set_request_id(rid)
    started = time.monotonic()
    try:
        try:
            response = await call_next(request)
        except StarletteHTTPException:
            raise
        except Exception:
            http_unhandled_total.inc()
            emit_event(
                "http_unhandled",
                level=logging.ERROR,
                method=request.method,
                path=request.url.path,
                error_type="INTERNAL",
            )
            logger.exception(
                "http_unhandled method=%s path=%s request_id=%s",
                request.method,
                request.url.path,
                rid,
            )
            return JSONResponse(
                {"detail": "internal error"},
                status_code=500,
                headers={"X-Request-Id": rid},
            )
        response.headers["X-Request-Id"] = rid
        if request.url.path not in _QUIET_PATHS:
            emit_event(
                "http_request",
                level=logging.DEBUG,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        return response
    finally:
        reset_request_id(token)


def _thread_prefix(user_id: str) -> str:
    """按用户生成 thread_id 前缀，天然做到「按用户隔离会话」，不用额外建映射表。"""
    return f"{user_id}-"


def _to_turn_response(result: TurnResult) -> TurnResponse:
    return TurnResponse.model_validate(result.model_dump())


def _raise_http_error(exc: TurnServiceError) -> None:
    if exc.code == "THREAD_FORBIDDEN":
        status_code = 403
    elif exc.code in {"THREAD_NOT_FOUND", "THREAD_NOT_PENDING"}:
        status_code = 404
    elif exc.code in {"THREAD_PENDING", "THREAD_BUSY"}:
        status_code = 409
    else:
        status_code = 422
    raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """进程活着即可；不查数据库。"""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    """能连上 Postgres 才算就绪。"""
    ok, reason = postgres_ready()
    if not ok:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok", "postgres": reason})


@app.get("/metrics")
def metrics() -> Response:
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)


@app.post("/turns", response_model=TurnResponse)
def create_turn(
    body: TurnRequest,
    user_id: str = Depends(get_current_user_id),
) -> TurnResponse:
    """跑一轮；``thread_id`` 留空则新建（前缀为当前工号），否则必须是自己名下的会话。"""
    try:
        result = _turn_service.turn(
            body.message, thread_id=body.thread_id, user_id=user_id
        )
    except TurnServiceError as exc:
        _raise_http_error(exc)

    return _to_turn_response(result)


@app.get("/turns/{thread_id}", response_model=TurnResponse)
def get_turn(
    thread_id: str,
    user_id: str = Depends(get_current_user_id),
) -> TurnResponse:
    """
    纯查询当前状态，不触发任何执行——用于页面刷新/换设备后重新拿回 interrupt 载荷或最终结果。
    """
    try:
        result = _turn_service.status(thread_id=thread_id, user_id=user_id)
    except TurnServiceError as exc:
        _raise_http_error(exc)
    return _to_turn_response(result)


@app.post("/turns/{thread_id}/resume", response_model=TurnResponse)
def resume_turn(
    thread_id: str,
    body: ResumeRequest,
    user_id: str = Depends(get_current_user_id),
) -> TurnResponse:
    """用给定答案续跑一步；该会话当前没有待回答问题时返回 404。"""
    try:
        result = _turn_service.resume(
            body.answer,
            thread_id=thread_id,
            user_id=user_id,
            return_current_if_completed=False,
        )
    except TurnServiceError as exc:
        _raise_http_error(exc)
    return _to_turn_response(result)


@app.get("/sessions", response_model=list[SessionSummary])
def get_sessions(
    limit: int = 20,
    user_id: str = Depends(get_current_user_id),
) -> list[SessionSummary]:
    """列出当前用户名下最近的会话（按 thread_id 前缀过滤）。"""
    prefix = _thread_prefix(user_id)
    sessions = list_sessions(limit=max(limit * 5, 50))
    mine = [s for s in sessions if s.thread_id.startswith(prefix)][:limit]
    return [
        SessionSummary(
            thread_id=s.thread_id,
            updated_at=s.updated_at,
            preview=s.preview,
            pending=s.pending,
        )
        for s in mine
    ]


def _user_config_view(user_id: str) -> UserConfigView:
    return UserConfigView(
        debug_mode=get_debug_mode(user_id),
        version_space=get_version_space(user_id),
    )


@app.get("/users/me/config", response_model=UserConfigView)
def get_my_config(user_id: str = Depends(get_current_user_id)) -> UserConfigView:
    """读当前用户的个人偏好；未设置过的字段为 null。"""
    return _user_config_view(user_id)


@app.patch("/users/me/config", response_model=UserConfigView)
def patch_my_config(
    body: UserConfigPatch,
    user_id: str = Depends(get_current_user_id),
) -> UserConfigView:
    """合并写入当前用户的个人偏好；未传入的字段保持不变。"""
    updates = body.model_dump(exclude_unset=True)
    if "debug_mode" in updates and updates["debug_mode"] is not None:
        set_debug_mode(user_id, bool(updates["debug_mode"]))
    if "version_space" in updates:
        set_version_space(user_id, updates["version_space"])
    return _user_config_view(user_id)


if _mcp_mount is not None:
    app.mount("/", _mcp_mount.app, name="mcp")
