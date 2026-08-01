"""
对外 CLI / lesson 调用图的薄封装：统一 empty_state、HITL 循环、结果展示数据。
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from langgraph.types import Command

from work_agent.core.checkpoint import get_checkpointer, make_thread_config
from work_agent.graph.main_graph import build_graph

# ask 收到的是 interrupt 的原始载荷列表，怎么展示交给调用方
AskFn = Callable[[list[Any]], str]

# HITL 轮次上限：用户一直回无效值时（比如组网始终为空）会反复 interrupt，
# 没有上限就是死循环。命中说明要么用户在乱试，要么节点的校验有 bug。
_MAX_HITL_ROUNDS = 20


def empty_state(text: str) -> dict[str, Any]:
    """
    每轮的输入。这里刻意把「所有」字段都显式置空。

    带 checkpointer 时，invoke 的入参会与上一轮快照合并，普通字段是后写覆盖，
    所以只有在这里出现的字段才会被清掉。新增 state 字段时务必同步加进来，
    否则上一轮的 run_id / results 会串到下一轮，答出上一轮的数据。
    """
    return {
        "task_id": "",
        "user_input": text,
        "intent": "",
        "requirement": "",
        "analysis_path": "",
        "cases": [],
        "exec_params": {},
        "run_id": "",
        "run_status": "",
        "poll_count": 0,
        "results": [],
        "logs": "",
        "report_path": "",
        "summary": {},
        "reply": "",
        "audit": [],
    }


def interrupt_payloads(result: dict[str, Any]) -> list[Any]:
    items = result.get("__interrupt__") or []
    return [getattr(item, "value", item) for item in items]


def run_turn(
    text: str,
    *,
    thread_id: str | None = None,
    ask: AskFn | None = None,
    with_checkpoint: bool = True,
) -> dict[str, Any]:
    """
    跑一轮用户输入；若遇到 interrupt，用 ask() 取回答并 resume。

    ask 拿到的是原始载荷（dict 列表），CLI 渲染成面板，
    测试里按 type 判断该回什么。
    """
    tid = thread_id or f"cli-{uuid.uuid4().hex[:8]}"
    app = build_graph(
        checkpointer=get_checkpointer() if with_checkpoint else None
    )
    config = make_thread_config(tid) if with_checkpoint else None

    result = app.invoke(empty_state(text), config=config)

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
        result = app.invoke(Command(resume=reply), config=config)

    result["_thread_id"] = tid
    return result
