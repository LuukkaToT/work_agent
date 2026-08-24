"""pytest 会话护栏与 Postgres 测试库夹具。

生产 ``POSTGRES_DSN`` 与测试 ``POSTGRES_TEST_DSN`` 必须是不同 database。
凡读写台账 / 个人配置 / checkpoint 的用例通过本文件夹具连测试库；
未配置或不可达时这些用例 skip，不碰库的单测照常跑。
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

import work_agent.core.checkpoint as checkpoint_mod
import work_agent.core.db as db_mod
import work_agent.core.ledger as ledger_mod
import work_agent.core.user_config as user_config_mod
from work_agent.core.config import get_settings
from work_agent.core.db_init import init_database


def _test_dsn() -> str:
    return (get_settings().postgres_test_dsn or "").strip()


def _dsn_reachable(dsn: str) -> bool:
    if not dsn:
        return False
    try:
        pool = ConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=1,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=False,
        )
        pool.open(wait=True, timeout=5)
        pool.close()
    except Exception:  # noqa: BLE001
        return False
    return True


def pytest_sessionstart(session: pytest.Session) -> None:
    """POSTGRES_TEST_DSN 若与生产 DSN 相同，直接退出，避免测到生产库。"""
    get_settings.cache_clear()
    settings = get_settings()
    prod = (settings.postgres_dsn or "").strip()
    test = (settings.postgres_test_dsn or "").strip()
    if prod and test and prod == test:
        pytest.exit(
            "POSTGRES_TEST_DSN 不能与 POSTGRES_DSN 相同，拒绝用生产库跑测试",
            returncode=2,
        )


@pytest.fixture(scope="session")
def pg_test_dsn() -> str:
    """会话内只探测一次测试库，不可达则 skip 所有依赖它的用例。"""
    get_settings.cache_clear()
    dsn = _test_dsn()
    if not _dsn_reachable(dsn):
        pytest.skip("POSTGRES_TEST_DSN 未配置，或测试库不可达")
    init_database(dsn)
    return dsn


@pytest.fixture
def pg_env(monkeypatch, pg_test_dsn: str) -> str:
    """把进程内生效 DSN 换成测试库，并清掉连接池 / 单例缓存。"""
    patched = replace(get_settings(), postgres_dsn=pg_test_dsn)
    monkeypatch.setattr(db_mod, "get_settings", lambda: patched)
    db_mod.reset_pool_cache()
    checkpoint_mod.get_checkpointer.cache_clear()
    ledger_mod.get_ledger.cache_clear()
    user_config_mod.get_user_config_store.cache_clear()
    yield pg_test_dsn
    db_mod.reset_pool_cache()
    checkpoint_mod.get_checkpointer.cache_clear()
    ledger_mod.get_ledger.cache_clear()
    user_config_mod.get_user_config_store.cache_clear()


@pytest.fixture
def pg_pool(pg_env: str):
    return db_mod.get_pool()


@pytest.fixture
def pg_ledger(pg_env: str):
    ledger_mod.get_ledger.cache_clear()
    return ledger_mod.get_ledger()


@pytest.fixture
def pg_user_config(pg_env: str):
    user_config_mod.get_user_config_store.cache_clear()
    return user_config_mod.get_user_config_store()


@pytest.fixture
def make_user_id(pg_pool):
    """生成隔离工号；用例结束时清掉该工号下的 pipelines / user_config。"""
    ids: list[str] = []

    def _make() -> str:
        uid = f"pytest-{uuid.uuid4().hex[:12]}"
        ids.append(uid)
        return uid

    yield _make
    if not ids:
        return
    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM pipelines WHERE user_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM user_config WHERE user_id = ANY(%s)", (ids,))


@pytest.fixture
def test_user_id(make_user_id) -> str:
    return make_user_id()
