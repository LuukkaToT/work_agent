"""
Postgres 集成测试：需要真实实例（本地用 ``docker compose up -d`` 起，见 docker-compose.yml）。

模块级 skipif：``POSTGRES_DSN`` 未配置或连不上时整份文件自动跳过，
不阻塞没有本地 Postgres 的机器 / CI。这里不 mock 任何东西——
就是要证明 checkpointer / ledger / user_config 在真实库上端到端能跑通。
"""

from __future__ import annotations

import uuid
from typing import TypedDict

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

import work_agent.core.checkpoint as checkpoint_mod
import work_agent.core.db as db_mod
import work_agent.core.identity as identity_mod
import work_agent.core.ledger as ledger_mod
import work_agent.core.user_config as user_config_mod
from work_agent.core.config import get_settings
from work_agent.core.user_config import PostgresUserConfigStore


def _pg_reachable() -> bool:
    if not get_settings().postgres_dsn:
        return False
    try:
        db_mod.get_pool()
    except Exception:  # noqa: BLE001
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="POSTGRES_DSN 未配置，或本地 Postgres 不可达（docker compose up -d）",
)


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


@pytest.fixture()
def pg_pool():
    return db_mod.get_pool()


@pytest.fixture()
def fresh_checkpointer():
    """每个测试拿一份新建的 PostgresSaver（清缓存，逼它重新走 get_checkpointer 逻辑）。"""
    checkpoint_mod.get_checkpointer.cache_clear()
    saver = checkpoint_mod.get_checkpointer()
    yield saver
    checkpoint_mod.get_checkpointer.cache_clear()


@pytest.fixture()
def pg_ledger():
    ledger_mod.get_ledger.cache_clear()
    ledger = ledger_mod.get_ledger()
    yield ledger
    ledger_mod.get_ledger.cache_clear()


@pytest.fixture()
def pg_user_config():
    user_config_mod.get_user_config_store.cache_clear()
    store = user_config_mod.get_user_config_store()
    yield store
    user_config_mod.get_user_config_store.cache_clear()


def _cleanup_thread(pool, thread_id: str) -> None:
    with pool.connection() as conn:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            conn.execute(f"DELETE FROM {table} WHERE thread_id=%s", (thread_id,))


def _cleanup_pipelines(pool, pipeline_ids: list[str]) -> None:
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM pipelines WHERE pipeline_id = ANY(%s)", (pipeline_ids,)
        )


def _cleanup_user_config(pool, user_ids: list[str]) -> None:
    with pool.connection() as conn:
        conn.execute("DELETE FROM user_config WHERE user_id = ANY(%s)", (user_ids,))


def test_get_checkpointer_returns_postgres_saver_when_dsn_configured(
    fresh_checkpointer,
):
    assert isinstance(fresh_checkpointer, PostgresSaver)


def test_get_ledger_returns_postgres_ledger_when_dsn_configured(pg_ledger):
    assert isinstance(pg_ledger, ledger_mod.PostgresLedger)


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


def test_postgres_ledger_upsert_and_get_roundtrip(pg_pool, pg_ledger):
    pid = f"pg-it-{uuid.uuid4().hex[:8]}"
    try:
        pg_ledger.upsert(
            pipeline_id=pid,
            task_id="pg-it-task",
            case_names=["case_x"],
            version="27B",
            env="7.223.50.60",
            status="running",
            user_id="z00000001",
        )
        rec = pg_ledger.get(pid)
        assert rec is not None
        assert rec.status == "running"
        assert rec.user_id == "z00000001"

        pg_ledger.update_status(pid, status="finished")
        assert pg_ledger.get(pid).status == "finished"
    finally:
        _cleanup_pipelines(pg_pool, [pid])


def test_postgres_ledger_replace_id_swaps_primary_key(pg_pool, pg_ledger):
    old_id = f"local-{uuid.uuid4().hex[:8]}"
    new_id = f"pg-it-{uuid.uuid4().hex[:8]}"
    try:
        pg_ledger.upsert(
            pipeline_id=old_id,
            task_id="pg-it-task",
            case_names=["case_x"],
            version="27B",
            env="7.223.50.60",
            status="creating",
            user_id="z00000001",
        )
        pg_ledger.replace_id(old_id, new_id, status="created")

        assert pg_ledger.get(old_id) is None
        rec = pg_ledger.get(new_id)
        assert rec is not None
        assert rec.status == "created"
        assert rec.user_id == "z00000001"
    finally:
        _cleanup_pipelines(pg_pool, [old_id, new_id])


def test_postgres_ledger_user_id_isolation(pg_pool, pg_ledger):
    pid_a = f"pg-it-{uuid.uuid4().hex[:8]}"
    pid_b = f"pg-it-{uuid.uuid4().hex[:8]}"
    task_id = f"pg-it-task-{uuid.uuid4().hex[:8]}"
    try:
        pg_ledger.upsert(
            pipeline_id=pid_a,
            task_id=task_id,
            case_names=["c1"],
            version="27B",
            env="7.223.50.60",
            status="running",
            user_id="alice",
        )
        pg_ledger.upsert(
            pipeline_id=pid_b,
            task_id=task_id,
            case_names=["c2"],
            version="27B",
            env="7.223.60.11",
            status="running",
            user_id="bob",
        )

        # 传 user_id：只看到自己的
        alice_records = pg_ledger.find_by_task(task_id, user_id="alice")
        assert [r.pipeline_id for r in alice_records] == [pid_a]

        # 不传 user_id：不过滤，兼容当前单用户 CLI 行为
        all_records = pg_ledger.find_by_task(task_id)
        assert {r.pipeline_id for r in all_records} == {pid_a, pid_b}

        # get() 附加归属校验：用别人的 user_id 查不到
        assert pg_ledger.get(pid_a, user_id="bob") is None
        assert pg_ledger.get(pid_a, user_id="alice") is not None
    finally:
        _cleanup_pipelines(pg_pool, [pid_a, pid_b])


def test_postgres_ledger_backfills_legacy_empty_user_id(pg_pool, pg_ledger):
    """身份接线前留下的历史空 user_id 记录：下次构造 PostgresLedger 时应被回填。"""
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
            # user_id 留空：模拟身份接线前留下的历史记录
        )
        assert pg_ledger.get(pid).user_id == ""

        # 重新构造一个新实例（不走 get_ledger 单例缓存）：_ensure_table 里的
        # 回填该跑一次，把这条历史记录改成默认身份
        fresh = ledger_mod.PostgresLedger()
        rec = fresh.get(pid)
        assert rec is not None
        assert rec.user_id == identity_mod.DEFAULT_USER_ID
    finally:
        _cleanup_pipelines(pg_pool, [pid])


def test_get_user_config_store_returns_postgres_when_dsn_configured(pg_user_config):
    assert isinstance(pg_user_config, PostgresUserConfigStore)


def test_postgres_user_config_crud_and_merge(pg_pool, pg_user_config):
    uid = f"pg-uc-{uuid.uuid4().hex[:8]}"
    try:
        assert pg_user_config.get(uid) == {}

        pg_user_config.update(uid, debug_mode=True)
        assert pg_user_config.get(uid) == {"debug_mode": True}

        # 合并语义：新字段不覆盖已有字段
        pg_user_config.update(uid, theme="dark")
        assert pg_user_config.get(uid) == {"debug_mode": True, "theme": "dark"}

        pg_user_config.update(uid, debug_mode=False)
        assert pg_user_config.get(uid)["debug_mode"] is False
        assert pg_user_config.get(uid)["theme"] == "dark"
    finally:
        _cleanup_user_config(pg_pool, [uid])


def test_postgres_user_config_users_are_isolated(pg_pool, pg_user_config):
    uid_a = f"pg-uc-{uuid.uuid4().hex[:8]}"
    uid_b = f"pg-uc-{uuid.uuid4().hex[:8]}"
    try:
        pg_user_config.update(uid_a, debug_mode=True)
        pg_user_config.update(uid_b, debug_mode=False)

        assert pg_user_config.get(uid_a) == {"debug_mode": True}
        assert pg_user_config.get(uid_b) == {"debug_mode": False}
    finally:
        _cleanup_user_config(pg_pool, [uid_a, uid_b])
