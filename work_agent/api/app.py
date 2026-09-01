"""
FastAPI 网关：把 runtime 的两阶段 HITL 包成 HTTP 接口。

- ``POST /turns``：跑一轮；没有 interrupt 就直接给最终结果，遇到 interrupt 就把
  载荷原样带回去，不在服务端阻塞等答案。
- ``GET /turns/{thread_id}``：纯查询当前状态，不触发执行——页面刷新/换设备后
  重新拿回 interrupt 载荷或最终结果用这个，不用只靠客户端缓存 POST 的响应。
- ``POST /turns/{thread_id}/resume``：客户端拿到 interrupt 后，把答案带过来续跑一步。
- ``GET /sessions``：列出当前用户名下的会话（按 thread_id 前缀过滤）。
- ``GET/PATCH /users/me/config``：读写个人偏好（``debug_mode``、``version_space``）。

鉴权是 mock 的（见 ``work_agent.api.identity``），公司侧鉴权接入前先用这层跑通流程。
同一个 thread_id 同一时刻只允许一个请求在跑，用进程内内存锁做非阻塞互斥，
抢不到直接 409，不排队等（见 ``work_agent.api.locks``）。

本地起服务：``uvicorn work_agent.api.app:app --reload``。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException

# 保留模块级 runtime 引用，现有网关测试和调用方可继续 monkeypatch 同一模块对象。
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
from work_agent.core.sessions import list_sessions
from work_agent.core.db_init import verify_schema_and_seed
from work_agent.core.user_config import (
    get_debug_mode,
    get_version_space,
    set_debug_mode,
    set_version_space,
)
from work_agent.mcp.http import build_mcp_http_mount
from work_agent.service.turns import TurnResult, TurnService, TurnServiceError

_turn_service = TurnService()
_mcp_mount = build_mcp_http_mount(get_settings(), turn_service=_turn_service)

@asynccontextmanager
async def _lifespan(_: FastAPI):
    verify_schema_and_seed()
    if _mcp_mount is None:
        yield
        return
    # 被 Mount 包裹的子应用不会自动运行 lifespan，必须由父应用启动 manager。
    async with _mcp_mount.server.session_manager.run():
        yield


app = FastAPI(title="work_agent gateway", version="0.2.0", lifespan=_lifespan)


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
    # 台账里的 thread 是混合全体用户的，多拉一点再按前缀过滤，避免 limit 用完全是别人的
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


# 必须最后挂载：Mount("/") 会匹配全部路径，放前面会遮住现有 REST 路由。
if _mcp_mount is not None:
    app.mount("/", _mcp_mount.app, name="mcp")
