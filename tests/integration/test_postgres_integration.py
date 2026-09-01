"""
Postgres 集成测试：只连 ``POSTGRES_TEST_DSN``，绝不碰生产 ``POSTGRES_DSN``。

台账 / 个人配置的 CRUD 在 ``test_ledger.py`` / ``test_user_config.py``。
这里只覆盖 checkpointer 跨实例读回，以及 init-db 对空 user_id 的回填。
未配测试库时由 ``pg_test_dsn`` 夹具 skip。
"""

from __future__ import annotations

import uuid
from typing import TypedDict

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

import work_agent.core.checkpoint as checkpoint_mod
import work_agent.core.identity as identity_mod
from work_agent.core.db_init import init_database
from work_agent.core.db_init import latest_schema_version


class _CounterState(TypedDict):
    count: int


def _increment(state: _CounterState) -> dict:
    return {"count": state["count"] + 1}


def _build_counter_graph():
    """图和业务无关，只用来验证 checkpointer 真的把状态存进/读出了 Postgres。"""
    graph = StateGraph(_CounterState)
    graph.add_node("inc", _increment)
    graph.add_edge(START, "inc")
    graph.add_edge("inc", END)
    return graph


def _cleanup_thread(pool, thread_id: str) -> None:
    with pool.connection() as conn:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            conn.execute(f"DELETE FROM {table} WHERE thread_id=%s", (thread_id,))


def _cleanup_pipelines(pool, pipeline_ids: list[str]) -> None:
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM pipelines WHERE pipeline_id = ANY(%s)", (pipeline_ids,)
        )


@pytest.fixture()
def fresh_checkpointer(pg_env):
    """每个测试拿一份新建的 PostgresSaver（清缓存，逼它重新走 get_checkpointer 逻辑）。"""
    checkpoint_mod.get_checkpointer.cache_clear()
    saver = checkpoint_mod.get_checkpointer()
    yield saver
    checkpoint_mod.get_checkpointer.cache_clear()


def test_get_checkpointer_returns_postgres_saver_when_dsn_configured(
    fresh_checkpointer,
):
    assert isinstance(fresh_checkpointer, PostgresSaver)


def test_postgres_checkpointer_persists_state_across_reinstantiation(
    pg_pool, fresh_checkpointer
):
    """模拟"新进程"重新拿 checkpointer：同一个 thread_id 应该还能读到之前存的状态。"""
    thread_id = f"pg-it-thread-{uuid.uuid4().hex[:8]}"
    config = checkpoint_mod.make_thread_config(thread_id)
    graph = _build_counter_graph()
    app = graph.compile(checkpointer=fresh_checkpointer)
    try:
        app.invoke({"count": 0}, config=config)
        snap = app.get_state(config)
        assert snap.values["count"] == 1

        checkpoint_mod.get_checkpointer.cache_clear()
        saver2 = checkpoint_mod.get_checkpointer()
        app2 = graph.compile(checkpointer=saver2)
        snap2 = app2.get_state(config)
        assert snap2.values["count"] == 1
    finally:
        _cleanup_thread(pg_pool, thread_id)


def test_query_recent_threads_and_exists_against_postgres(pg_pool, fresh_checkpointer):
    thread_id = f"pg-it-thread-{uuid.uuid4().hex[:8]}"
    config = checkpoint_mod.make_thread_config(thread_id)
    graph = _build_counter_graph()
    app = graph.compile(checkpointer=fresh_checkpointer)
    try:
        app.invoke({"count": 0}, config=config)

        assert checkpoint_mod.thread_checkpoint_exists(fresh_checkpointer, thread_id)
        assert not checkpoint_mod.thread_checkpoint_exists(
            fresh_checkpointer, "no-such-thread"
        )

        rows = checkpoint_mod.query_recent_threads(fresh_checkpointer, limit=50)
        assert thread_id in {tid for tid, _ in rows}
    finally:
        _cleanup_thread(pg_pool, thread_id)


def test_init_db_backfills_legacy_empty_user_id(pg_pool, pg_ledger, pg_test_dsn):
    """空 user_id 由 init-db 的 schema.sql 回填，不再在 Ledger 构造时偷偷 UPDATE。"""
    pid = f"pg-it-{uuid.uuid4().hex[:8]}"
    task_id = f"pg-it-task-{uuid.uuid4().hex[:8]}"
    try:
        pg_ledger.upsert(
            pipeline_id=pid,
            task_id=task_id,
            case_names=["c1"],
            version="27B",
            env="7.223.50.60",
            status="running",
        )
        assert pg_ledger.get(pid).user_id == ""

        init_database(pg_test_dsn)
        rec = pg_ledger.get(pid)
        assert rec is not None
        assert rec.user_id == identity_mod.DEFAULT_USER_ID
    finally:
        _cleanup_pipelines(pg_pool, [pid])


def test_latest_business_schema_migration_is_recorded(pg_pool):
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=%s",
            (latest_schema_version(),),
        ).fetchone()
        columns = {
            item["column_name"]
            for item in conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name='logic_topologies'
                """
            ).fetchall()
        }
    assert row is not None
    assert {"id", "name", "constraint_value", "config", "aliases"} <= columns
