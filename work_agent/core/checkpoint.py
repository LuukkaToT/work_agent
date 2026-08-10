"""
SQLite Checkpointer：把图的中间状态持久化到本地文件。

没有 checkpointer：
  invoke 跑完，状态只在内存里，进程结束就没了。

有了 checkpointer + thread_id：
  每一步节点后都会存档；下次用同一个 thread_id 还能 get_state / 继续跑。
  这是 interrupt（人工确认）和「跨进程续跑」的前提。
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

from langgraph.checkpoint.sqlite import SqliteSaver

from work_agent.core.config import get_settings


@lru_cache(maxsize=1)
def get_checkpointer() -> SqliteSaver:
    """
    进程内复用同一个 SqliteSaver。

    注意：连接必须一直开着；若用 from_conn_string 的 with 块，退出 with 后
    连接会关，后续 get_state 会失败。所以这里手动建连并缓存。
    """
    path = get_settings().checkpoint_path
    path.parent.mkdir(parents=True, exist_ok=True)

    # check_same_thread=False：允许 LangGraph 在不同线程读库（官方示例写法）
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()  # 建表（若不存在）
    return saver


def make_thread_config(thread_id: str) -> dict:
    """
    构造 invoke / get_state 用的 config。

    参数:
        thread_id: 会话主键。

    返回:
        ``{"configurable": {"thread_id": ...}}``。
    """
    return {"configurable": {"thread_id": thread_id}}
