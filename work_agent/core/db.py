"""
Postgres 连接池：checkpointer / ledger / user_config 共用一个进程内连接池。

不用 SQLAlchemy：这里只需要「一个可复用连接池 + 少量原生 SQL」，
LangGraph 的 PostgresSaver 本身也直接吃 psycopg 的 Connection / ConnectionPool，
再套一层 SQLAlchemy engine 反而要多写一层适配，不划算。
"""

from __future__ import annotations

from functools import lru_cache

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from work_agent.core.config import get_settings


@lru_cache(maxsize=1)
def get_pool() -> ConnectionPool:
    """
    返回进程内共享的 Postgres 连接池（首次调用时才真正建连）。

    row_factory 统一用 dict_row：langgraph 的 PostgresSaver 要求连接是
    ``Connection[dict]``；ledger 这边用 dict 取列也方便，
    两边共用同一个池、同一套访问方式。

    返回:
        已 open 并等待就绪的 ConnectionPool。

    异常:
        RuntimeError: 未配置 POSTGRES_DSN（应用连生产库；测试请配 POSTGRES_TEST_DSN
            并由 pytest 把生效 DSN 换成测试库，见 tests/conftest.py）。
        psycopg 相关异常: DSN 配了但连不上（网络 / 账号密码错误），不静默吞掉。
    """
    dsn = get_settings().postgres_dsn
    if not dsn:
        raise RuntimeError(
            "POSTGRES_DSN 未配置：请在 .env 里设置生产库连接串，"
            "例如 postgresql://user:pass@host:5432/work_agent。"
            "测试库用 POSTGRES_TEST_DSN，二者必须是不同的 database。"
        )
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=10,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    )
    pool.open(wait=True, timeout=10)
    return pool


def reset_pool_cache() -> None:
    """测试 / 重连专用：清掉缓存的连接池（不主动 close，交给调用方决定）。"""
    get_pool.cache_clear()
