"""
Checkpointer：把图的中间状态持久化到 SQLite 或 Postgres。

没有 checkpointer：
  invoke 跑完，状态只在内存里，进程结束就没了。

有了 checkpointer + thread_id：
  每一步节点后都会存档；下次用同一个 thread_id 还能 get_state / 继续跑。
  这是 interrupt（人工确认）和「跨进程续跑」的前提。

后端选择：``POSTGRES_DSN`` 配了就用 PostgresSaver（上线用，多进程/多副本共享同一份
状态）；没配就退回本地 SqliteSaver（本地开发 / 单测，不依赖真实数据库）。
两种后端的 checkpoints 表结构等价（thread_id / checkpoint_ns / checkpoint_id），
但拿原始连接的方式和占位符语法不同，这层差异封在 query_recent_threads /
thread_checkpoint_exists 里，调用方（sessions.py）不用关心用的是哪个库。
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from work_agent.core.config import get_settings
from work_agent.core.db import get_pool


@lru_cache(maxsize=1)
def get_checkpointer() -> BaseCheckpointSaver:
    """
    进程内复用同一个 checkpointer；按 ``POSTGRES_DSN`` 是否配置自动选后端。

    注意：两种后端都要求连接（或连接池）一直存活，不能用完就关，
    所以这里手动建连并靠 lru_cache 常驻，而不是每次用 with 块。
    """
    settings = get_settings()
    if settings.postgres_dsn:
        saver = PostgresSaver(get_pool())
        saver.setup()  # 建表（若不存在），幂等
        return saver

    path = settings.checkpoint_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：允许 LangGraph 在不同线程读库（官方示例写法）
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
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
    sql_pg = """
        SELECT thread_id, MAX(checkpoint_id) AS last_id
        FROM checkpoints
        WHERE checkpoint_ns = ''
        GROUP BY thread_id
        ORDER BY last_id DESC
        LIMIT %s
    """
    sql_sqlite = sql_pg.replace("%s", "?")

    if isinstance(saver, PostgresSaver):
        with get_pool().connection() as conn:
            rows = conn.execute(sql_pg, (limit,)).fetchall()
        return [(r["thread_id"], str(r["last_id"] or "")) for r in rows]

    rows = saver.conn.execute(sql_sqlite, (limit,)).fetchall()
    return [(r[0], str(r[1] or "")) for r in rows]


def thread_checkpoint_exists(saver: BaseCheckpointSaver, thread_id: str) -> bool:
    """
    判断某 thread 是否已在 checkpointer 里落过盘（主图，``checkpoint_ns`` 为空）。

    参数:
        saver: ``get_checkpointer()`` 返回的实例。
        thread_id: 会话 id。

    返回:
        存在主图 checkpoint 则为 True。
    """
    if not thread_id:
        return False

    sql_pg = """
        SELECT 1 FROM checkpoints
        WHERE thread_id = %s AND checkpoint_ns = ''
        LIMIT 1
    """
    sql_sqlite = sql_pg.replace("%s", "?")

    if isinstance(saver, PostgresSaver):
        with get_pool().connection() as conn:
            row = conn.execute(sql_pg, (thread_id,)).fetchone()
        return row is not None

    row = saver.conn.execute(sql_sqlite, (thread_id,)).fetchone()
    return row is not None
