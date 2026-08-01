"""
M10：Checkpointer + thread_id 断点续跑。

演示两步（同一进程内模拟「跑到一半停住 → 稍后再继续」）：
  1) 带 interrupt_before=["collect_results"] 启动，图在收结果前暂停
  2) 用同一个 thread_id 再 invoke(None, config)，从断点接着跑完

    python -m work_agent.lessons.09_checkpoint

想验证「跨进程」：先跑本脚本看 thread_id，再开另一个终端执行
（模块名以数字开头，不能用普通 from import，要用 importlib）：

    python -c "import importlib; m=importlib.import_module('work_agent.lessons.09_checkpoint'); m.show_state('把thread_id粘这里')"
"""

from __future__ import annotations

import uuid

from work_agent.core.checkpoint import get_checkpointer, make_thread_config
from work_agent.core.config import get_settings
from work_agent.graph.main_graph import build_graph


def empty_state(text: str) -> dict:
    return {
        "task_id": "",
        "user_input": text,
        "intent": "",
        "requirement": "",
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


def show_state(thread_id: str) -> None:
    """只读：从 SQLite 取出某个 thread 的最新快照。"""
    app = build_graph(checkpointer=get_checkpointer())
    config = make_thread_config(thread_id)
    snap = app.get_state(config)
    print("thread_id:", thread_id)
    print("next     :", snap.next)  # 空 tuple 表示已结束；非空表示停在谁前面
    values = snap.values or {}
    print("task_id  :", values.get("task_id"))
    print("run_id   :", values.get("run_id"))
    print("run_status:", values.get("run_status"))
    print("report   :", values.get("report_path"))
    print("audit    :", [a.get("step") for a in (values.get("audit") or [])])


def main() -> None:
    settings = get_settings()
    print("checkpoint db:", settings.checkpoint_path)

    # 编译时挂上 checkpointer，并在进入 collect_results「之前」打断
    app = build_graph(
        checkpointer=get_checkpointer(),
        interrupt_before=["collect_results"],
    )

    thread_id = f"demo-{uuid.uuid4().hex[:8]}"
    config = make_thread_config(thread_id)
    user_text = "执行用例 case_downlink_001，版本 27B，组网 topo_a"

    print("\n=== 第 1 次 invoke：跑到 collect_results 前暂停 ===")
    print("thread_id:", thread_id)
    app.invoke(empty_state(user_text), config=config)

    snap1 = app.get_state(config)
    print("paused next :", snap1.next)  # 期望: ('collect_results',)
    print("run_id      :", (snap1.values or {}).get("run_id"))
    print("run_status  :", (snap1.values or {}).get("run_status"))
    print("audit so far:", [a.get("step") for a in (snap1.values or {}).get("audit") or []])
    print("report yet? :", (snap1.values or {}).get("report_path") or "(还没有)")

    print("\n=== 第 2 次 invoke(None)：从断点继续 ===")
    # 输入传 None =「不要新开一轮，接着上次的 checkpoint 往下跑」
    app.invoke(None, config=config)

    snap2 = app.get_state(config)
    print("next after resume:", snap2.next)  # 期望: ()
    print("report_path      :", (snap2.values or {}).get("report_path"))
    print("audit final      :", [a.get("step") for a in (snap2.values or {}).get("audit") or []])

    print("\n提示：用下面命令可在新进程里只读同一 thread：")
    print(
        "  python -c \"import importlib; "
        "m=importlib.import_module('work_agent.lessons.09_checkpoint'); "
        f"m.show_state({thread_id!r})\""
    )


if __name__ == "__main__":
    main()
