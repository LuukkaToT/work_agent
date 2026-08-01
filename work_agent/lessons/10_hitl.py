"""
M11：interrupt 人机确认。

    python -m work_agent.lessons.10_hitl

流程：
  1) 故意不说组网 → 图 interrupt 问组网
  2) 你输入 topo_a
  3) 图再 interrupt 问是否执行
  4) 你输入 yes / no
"""

from __future__ import annotations

import uuid

from langgraph.types import Command

from work_agent.core.checkpoint import get_checkpointer, make_thread_config
from work_agent.graph.main_graph import build_graph


def empty_state(text: str) -> dict:
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


def print_interrupts(result: dict) -> None:
    """invoke 暂停时，提问内容在 result['__interrupt__'] 里。"""
    items = result.get("__interrupt__") or []
    for i, item in enumerate(items):
        value = getattr(item, "value", item)
        print(f"[interrupt #{i}] {value}")


def main() -> None:
    # HITL 必须挂 checkpointer；节点内 interrupt()，不必再设 interrupt_before
    app = build_graph(checkpointer=get_checkpointer())
    thread_id = f"hitl-{uuid.uuid4().hex[:8]}"
    config = make_thread_config(thread_id)

    # 故意不写组网，触发 ask_topology
    text = "执行用例 case_downlink_001，版本 27B"
    print("thread_id:", thread_id)
    print("user    :", text)

    result = app.invoke(empty_state(text), config=config)

    while result.get("__interrupt__"):
        print("-" * 50)
        print_interrupts(result)
        user_reply = input("你的回复 > ").strip()
        result = app.invoke(Command(resume=user_reply), config=config)

    print("-" * 50)
    print("最终 summary :", result.get("summary"))
    print("run_id       :", result.get("run_id"))
    print("report_path  :", result.get("report_path"))
    print("audit steps  :", [a.get("step") for a in (result.get("audit") or [])])


if __name__ == "__main__":
    main()
