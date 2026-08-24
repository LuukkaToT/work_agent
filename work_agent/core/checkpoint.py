"""
Checkpointer：把图的中间状态持久化到 Postgres。

没有 checkpointer：
  invoke 跑完，状态只在内存里，进程结束就没了。

有了 checkpointer + thread_id：
  每一步节点后都会存档；下次用同一个 thread_id 还能 get_state / 继续跑。
  这是 interrupt（人工确认）和「跨进程续跑」的前提。

表由 ``python -m work_agent init-db`` 创建（``PostgresSaver.setup()``），
本模块运行时不再建表。未配 ``POSTGRES_DSN`` 时 ``get_pool()`` 会显式报错。
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver

from work_agent.core.db import get_pool


@lru_cache(maxsize=1)
def get_checkpointer() -> BaseCheckpointSaver:
    """
    进程内复用同一个 Postgres checkpointer。

    连接池必须一直存活，不能用完就关，所以靠 lru_cache 常驻。
    """
    return PostgresSaver(get_pool())


def make_thread_config(thread_id: str) -> dict:
    """
    构造 invoke / get_state 用的 config。

    参数:
        thread_id: 会话主键。

    返回:
        ``{"configurable": {"thread_id": ...}}``。
    """
    return {"configurable": {"thread_id": thread_id}}


def query_recent_threads(
    saver: BaseCheckpointSaver, limit: int
) -> list[tuple[str, str]]:
    """
    列出最近更新的主图 thread（``checkpoint_ns`` 为空，即非子图）。

    参数:
        saver: ``get_checkpointer()`` 返回的实例。
        limit: 最多返回条数。

    返回:
        ``[(thread_id, last_checkpoint_id), ...]``，按 last_checkpoint_id 倒序。
    """
    sql = """
        SELECT thread_id, MAX(checkpoint_id) AS last_id
        FROM checkpoints
        WHERE checkpoint_ns = ''
        GROUP BY thread_id
        ORDER BY last_id DESC
        LIMIT %s
    """
    with get_pool().connection() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [(r["thread_id"], str(r["last_id"] or "")) for r in rows]


def thread_checkpoint_exists(saver: BaseCheckpointSaver, thread_id: str) -> bool:
    """
    判断某 thread 是否已在 checkpointer 里落过盘（主图，``checkpoint_ns`` 为空）。

    参数:
        saver: ``get_checkpointer()`` 返回的实例（签名保留，实际查共享连接池）。
        thread_id: 会话 id。

    返回:
        存在主图 checkpoint 则为 True。
    """
    if not thread_id:
        return False

    sql = """
        SELECT 1 FROM checkpoints
        WHERE thread_id = %s AND checkpoint_ns = ''
        LIMIT 1
    """
    with get_pool().connection() as conn:
        row = conn.execute(sql, (thread_id,)).fetchone()
    return row is not None
