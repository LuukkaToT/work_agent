"""
对外 CLI / lesson 调用图的薄封装：统一 empty_state、HITL 循环、结果展示数据。
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from langgraph.types import Command

from work_agent.core.checkpoint import get_checkpointer, make_thread_config
from work_agent.graph.main_graph import build_graph


def empty_state(text: str) -> dict[str, Any]:
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
        "audit": [],
    }


def interrupt_payloads(result: dict[str, Any]) -> list[Any]:
    items = result.get("__interrupt__") or []
    return [getattr(item, "value", item) for item in items]


def run_turn(
    text: str,
    *,
    thread_id: str | None = None,
    ask: Callable[[str], str] | None = None,
    with_checkpoint: bool = True,
) -> dict[str, Any]:
    """
    跑一轮用户输入；若遇到 interrupt，用 ask() 取回答并 resume。

    ask: 传入提示字符串，返回用户回复。CLI 里用 input；测试里可注入假函数。
    """
    tid = thread_id or f"cli-{uuid.uuid4().hex[:8]}"
    app = build_graph(
        checkpointer=get_checkpointer() if with_checkpoint else None
    )
    config = make_thread_config(tid) if with_checkpoint else None

    result = app.invoke(empty_state(text), config=config)
    while result.get("__interrupt__"):
        if ask is None:
            raise RuntimeError("图触发了 interrupt，但未提供 ask 回调")
        payloads = interrupt_payloads(result)
        prompt_lines = ["[需要你确认]"]
        for p in payloads:
            prompt_lines.append(str(p))
        prompt_lines.append("你的回复")
        reply = ask("\n".join(prompt_lines)).strip()
        result = app.invoke(Command(resume=reply), config=config)

    result["_thread_id"] = tid
    return result
