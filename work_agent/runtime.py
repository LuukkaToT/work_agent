"""
对外 CLI / 调用图的薄封装：HITL 循环、thread 管理、可选进度事件。

调用方只传本轮用户话；任务级字段的重置由 intake 负责，
这里不再维护一份 empty_state 清单。
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from work_agent.core.checkpoint import (
    get_checkpointer,
    make_thread_config,
    thread_checkpoint_exists,
)
from work_agent.graph.helpers.progress import reset_progress_hook, set_progress_hook
from work_agent.graph.main_graph import build_graph

# ask 收到的是 interrupt 的原始载荷列表，怎么展示交给调用方
AskFn = Callable[[list[Any]], Any]
# 进度事件：node:intake / tool:fetch_logs / status:waiting_input
EventFn = Callable[[str], None]

# HITL 轮次上限：用户一直回无效值时（比如组网始终为空）会反复 interrupt，
# 没有上限就是死循环。命中说明要么用户在乱试，要么节点的校验有 bug。
_MAX_HITL_ROUNDS = 20


def interrupt_payloads(result: dict[str, Any]) -> list[Any]:
    """
    从 invoke 结果中取出 HITL interrupt 的原始载荷。

    参数:
        result: 图 invoke 返回的 dict（可能含 ``__interrupt__``）。

    返回:
        载荷列表（已展开 ``.value``）；无 interrupt 时为空列表。
    """
    items = result.get("__interrupt__") or []
    return [getattr(item, "value", item) for item in items]


def _interrupt_values_from_snapshot(snap: Any) -> list[Any]:
    """从 get_state 快照抽出 interrupt 载荷。"""
    out: list[Any] = []
    for task in getattr(snap, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            out.append(getattr(item, "value", item))
    interrupts = getattr(snap, "interrupts", None) or ()
    for item in interrupts:
        val = getattr(item, "value", item)
        if val not in out:
            out.append(val)
    return out


def _emit(on_event: EventFn | None, message: str) -> None:
    """若提供了 on_event 回调则转发一条进度消息。"""
    if on_event is not None:
        on_event(message)


def _stream_graph(
    app: Any,
    payload: Any,
    *,
    config: dict[str, Any] | None,
    on_event: EventFn | None,
) -> dict[str, Any]:
    """
    用 stream(updates) 跑图并上报节点名；返回近似 invoke 的结果 dict
    （含可选 ``__interrupt__``）。
    """
    final_values: dict[str, Any] = {}
    interrupt_objs: list[Any] = []

    if config is None:
        for item in app.stream(
            payload, config=None, stream_mode=["updates", "values"]
        ):
            mode, data = item
            if mode == "updates" and isinstance(data, dict):
                if "__interrupt__" in data:
                    interrupt_objs = list(data.get("__interrupt__") or ())
                    _emit(on_event, "status:waiting_input")
                else:
                    for name in data:
                        _emit(on_event, f"node:{name}")
            elif mode == "values" and isinstance(data, dict):
                final_values = data
    else:
        for chunk in app.stream(payload, config=config, stream_mode="updates"):
            if not isinstance(chunk, dict):
                continue
            if "__interrupt__" in chunk:
                interrupt_objs = list(chunk.get("__interrupt__") or ())
                _emit(on_event, "status:waiting_input")
                continue
            for name in chunk:
                _emit(on_event, f"node:{name}")
        snap = app.get_state(config)
        final_values = dict(snap.values or {})
        # stream 已给出 __interrupt__ 时沿用；否则仅在图仍挂起（next 非空）时补齐
        if not interrupt_objs and getattr(snap, "next", None):
            for task in getattr(snap, "tasks", ()) or ():
                interrupt_objs.extend(
                    list(getattr(task, "interrupts", ()) or ())
                )

    result = dict(final_values)
    if interrupt_objs:
        result["__interrupt__"] = interrupt_objs
    return result


def get_pending_interrupts(thread_id: str) -> list[Any]:
    """
    查询某 thread 是否停在 HITL。

    参数:
        thread_id: 会话 thread id；空字符串视为无 pending。

    返回:
        有未完成 interrupt 时返回载荷列表，否则空列表。
    """
    if not thread_id:
        return []
    app = build_graph(checkpointer=get_checkpointer())
    snap = app.get_state(make_thread_config(thread_id))
    if not snap:
        return []
    if not (snap.next or _interrupt_values_from_snapshot(snap)):
        return []
    payloads = _interrupt_values_from_snapshot(snap)
    return payloads


def get_turn_status(thread_id: str) -> dict[str, Any] | None:
    """
    查询某 thread 当前状态，纯读、不触发任何执行——安全重复调用（轮询/刷新页面/换设备）。

    专给 HTTP 网关的 ``GET /turns/{thread_id}`` 用：客户端拿到 interrupt 后如果
    刷新了页面或换了设备，原来那次 POST 响应里的载荷就丢了，得能重新问一遍
    「这个会话现在是什么状态」，而不是只能靠客户端自己缓存。

    参数:
        thread_id: 会话 id。

    返回:
        图当前状态 dict（同 ``run_turn_step`` 的返回形状，含 ``_thread_id``，
        pending 时含 ``__interrupt__``）；thread 从未落过 checkpoint 时返回 None。
    """
    if not thread_id:
        return None
    saver = get_checkpointer()
    if not thread_checkpoint_exists(saver, thread_id):
        return None

    app = build_graph(checkpointer=saver)
    snap = app.get_state(make_thread_config(thread_id))
    result = dict(snap.values or {})
    payloads = _interrupt_values_from_snapshot(snap)
    if payloads:
        result["__interrupt__"] = payloads
    result["_thread_id"] = thread_id
    return result


def resume_pending(
    thread_id: str,
    *,
    ask: AskFn,
    on_event: EventFn | None = None,
) -> dict[str, Any] | None:
    """
    若 thread 有未完成 interrupt，用 ask 续跑直到结束或再次需要输入。

    参数:
        thread_id: 要续跑的会话 id。
        ask: 收到 interrupt 载荷列表后返回用户回答的回调。
        on_event: 可选进度回调（node:… / tool:…）。

    返回:
        续跑完成后的图结果（含 ``_thread_id``）；无 pending 时返回 None。
        HITL 超过上限时抛 RuntimeError。
    """
    payloads = get_pending_interrupts(thread_id)
    if not payloads:
        return None

    app = build_graph(checkpointer=get_checkpointer())
    config = make_thread_config(thread_id)
    token = set_progress_hook(on_event)
    try:
        reply = ask(payloads).strip()
        result = _stream_graph(
            app, Command(resume=reply), config=config, on_event=on_event
        )

        rounds = 0
        while result.get("__interrupt__"):
            rounds += 1
            if rounds > _MAX_HITL_ROUNDS:
                raise RuntimeError(
                    f"HITL 交互已超过 {_MAX_HITL_ROUNDS} 轮仍未完成，中止本轮"
                )
            reply = ask(interrupt_payloads(result)).strip()
            result = _stream_graph(
                app, Command(resume=reply), config=config, on_event=on_event
            )
    finally:
        reset_progress_hook(token)

    result["_thread_id"] = thread_id
    return result


def run_turn(
    text: str,
    *,
    thread_id: str | None = None,
    user_id: str = "",
    ask: AskFn | None = None,
    with_checkpoint: bool = True,
    on_event: EventFn | None = None,
) -> dict[str, Any]:
    """
    跑一轮用户输入；若遇到 interrupt，用 ask() 取回答并 resume。

    ask 拿到的是原始载荷（dict 列表），CLI 渲染成面板，
    测试里按 type 判断该回什么。

    参数:
        text: 本轮用户自然语言输入。
        thread_id: 会话 id；None 时按 ``user_id``（或 ``cli``）自动生成。
        user_id: 当前操作者工号；随消息一起写进图 state（见 ``state.py``
            的 ``user_id`` 字段），每轮都传、幂等，不依赖"只在第一轮写"。
        ask: HITL 回调；图触发 interrupt 且未提供时抛 RuntimeError。
        with_checkpoint: False 时不挂 checkpointer（无持久化、无跨轮续跑）。
        on_event: 可选进度回调，事件形如 ``node:router`` / ``tool:fetch_logs``。

    返回:
        图最终 state（并写入 ``_thread_id``）。
    """
    tid = thread_id or new_thread_id(user_id or "cli")
    app = build_graph(
        checkpointer=get_checkpointer() if with_checkpoint else None
    )
    config = make_thread_config(tid) if with_checkpoint else None

    token = set_progress_hook(on_event)
    try:
        result = _stream_graph(
            app,
            {"messages": [HumanMessage(content=text)], "user_id": user_id},
            config=config,
            on_event=on_event,
        )

        rounds = 0
        while result.get("__interrupt__"):
            if ask is None:
                raise RuntimeError("图触发了 interrupt，但未提供 ask 回调")
            rounds += 1
            if rounds > _MAX_HITL_ROUNDS:
                raise RuntimeError(
                    f"HITL 交互已超过 {_MAX_HITL_ROUNDS} 轮仍未完成，中止本轮"
                )
            reply = ask(interrupt_payloads(result)).strip()
            result = _stream_graph(
                app,
                Command(resume=reply),
                config=config,
                on_event=on_event,
            )
    finally:
        reset_progress_hook(token)

    result["_thread_id"] = tid
    return result


def new_thread_id(prefix: str = "cli") -> str:
    """
    生成新的会话 thread id。

    参数:
        prefix: id 前缀；CLI 默认 ``cli``，HTTP 网关传工号当前缀
            （形如 ``z00888363-a1b2c3d4``），这样 ``GET /sessions``
            按前缀过滤就能天然做到「只看自己的会话」，不用额外建映射表。

    返回:
        形如 ``{prefix}-xxxxxxxx`` 的短 id。
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def run_turn_step(
    text: str,
    *,
    thread_id: str | None = None,
    user_id: str = "",
    on_event: EventFn | None = None,
) -> dict[str, Any]:
    """
    非阻塞版 ``run_turn``：只跑一步，遇到 interrupt 立刻返回，不在这里循环等答案。

    专给 HTTP 网关用——一次 HTTP 请求对应「跑到底，或者跑到第一个 interrupt 为止」，
    要不要继续问下一轮交给客户端决定（自己决定何时调 ``resume_step``）。
    CLI 继续用阻塞版 ``run_turn``（本地终端里 ``ask()`` 直接问人更省事，
    不用客户端自己维护「上次问到哪」的状态）。

    参数:
        text: 本轮用户自然语言输入。
        thread_id: 会话 id；None 时按 ``user_id``（或 ``cli``）自动生成
            （见 ``new_thread_id``）。
        user_id: 当前操作者工号；随消息一起写进图 state，每轮都传、幂等。
        on_event: 可选进度回调。

    返回:
        图 invoke 的结果 dict，额外带 ``_thread_id``；
        触发 interrupt 时含 ``__interrupt__``（原始载荷列表，未展开 ``.value``）。
    """
    tid = thread_id or new_thread_id(user_id or "cli")
    app = build_graph(checkpointer=get_checkpointer())
    config = make_thread_config(tid)

    token = set_progress_hook(on_event)
    try:
        result = _stream_graph(
            app,
            {"messages": [HumanMessage(content=text)], "user_id": user_id},
            config=config,
            on_event=on_event,
        )
    finally:
        reset_progress_hook(token)

    result["_thread_id"] = tid
    return result


def resume_step(
    thread_id: str,
    answer: Any,
    *,
    on_event: EventFn | None = None,
) -> dict[str, Any] | None:
    """
    非阻塞版 ``resume_pending``：只用给定答案续跑一步，遇到下一个 interrupt 立刻返回。

    参数:
        thread_id: 要续跑的会话 id。
        answer: 用户对上一个 interrupt 的回答。
        on_event: 可选进度回调。

    返回:
        图结果 dict（同 ``run_turn_step``）；该 thread 当前没有 pending interrupt
        时返回 None（调用方应回 404，而不是当成正常结果处理）。
    """
    if answer is None:
        return None
    if isinstance(answer, str) and not answer.strip():
        return None
    if isinstance(answer, (dict, list)) and not answer:
        return None

    payloads = get_pending_interrupts(thread_id)
    if not payloads:
        return None

    app = build_graph(checkpointer=get_checkpointer())
    config = make_thread_config(thread_id)
    token = set_progress_hook(on_event)
    try:
        normalized_answer = answer.strip() if isinstance(answer, str) else answer
        result = _stream_graph(
            app, Command(resume=normalized_answer), config=config, on_event=on_event
        )
    finally:
        reset_progress_hook(token)

    result["_thread_id"] = thread_id
    return result
