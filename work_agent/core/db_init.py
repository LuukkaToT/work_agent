"""
数据库初始化：执行 sql/schema.sql，并调用 PostgresSaver.setup() 建 checkpoint 表。

新环境：建空库 → 配 DSN → python -m work_agent init-db → 再启动 API。
运行时不再 CREATE TABLE / saver.setup()。
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from langgraph.checkpoint.postgres import PostgresSaver

from work_agent.core.config import project_root

_SCHEMA_PATH = project_root() / "sql" / "schema.sql"


def _statements(sql_text: str) -> list[str]:
    """按分号切开可执行语句，丢掉空段和纯注释段。"""
    out: list[str] = []
    for raw in sql_text.split(";"):
        lines = [
            line
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        stmt = "\n".join(lines).strip()
        if stmt:
            out.append(stmt)
    return out


def init_database(dsn: str) -> None:
    """
    对给定 DSN 建业务表 + LangGraph checkpoint 表。

    参数:
        dsn: Postgres 连接串；空串则报错。

    异常:
        RuntimeError: DSN 为空。
        psycopg 相关异常: 连不上或 SQL 失败，不吞。
    """
    if not (dsn or "").strip():
        raise RuntimeError(
            "未提供 Postgres DSN：生产用 POSTGRES_DSN（python -m work_agent init-db），"
            "测试用 POSTGRES_TEST_DSN（python -m work_agent init-db --test）"
        )

    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=4,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    )
    pool.open(wait=True, timeout=10)
    try:
        with pool.connection() as conn:
            for stmt in _statements(schema):
                conn.execute(stmt)
        PostgresSaver(pool).setup()
    finally:
        pool.close()
