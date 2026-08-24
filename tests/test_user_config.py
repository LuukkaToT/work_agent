"""core/user_config.py：连 POSTGRES_TEST_DSN 测配置合并语义。"""

from __future__ import annotations

from work_agent.core.user_config import (
    PostgresUserConfigStore,
    get_debug_mode,
    set_debug_mode,
)


def test_get_user_config_store_returns_postgres(pg_user_config):
    assert isinstance(pg_user_config, PostgresUserConfigStore)


def test_get_returns_empty_dict_for_unknown_user(pg_user_config, test_user_id):
    assert pg_user_config.get(test_user_id) == {}


def test_update_is_merge_not_replace(pg_user_config, test_user_id):
    pg_user_config.update(test_user_id, debug_mode=True)
    pg_user_config.update(test_user_id, theme="dark")
    assert pg_user_config.get(test_user_id) == {
        "debug_mode": True,
        "theme": "dark",
    }


def test_update_overwrites_same_key(pg_user_config, test_user_id):
    pg_user_config.update(test_user_id, debug_mode=True)
    pg_user_config.update(test_user_id, debug_mode=False)
    assert pg_user_config.get(test_user_id)["debug_mode"] is False


def test_users_are_isolated(pg_user_config, make_user_id):
    alice = make_user_id()
    bob = make_user_id()
    pg_user_config.update(alice, debug_mode=True)
    pg_user_config.update(bob, debug_mode=False)
    assert pg_user_config.get(alice) == {"debug_mode": True}
    assert pg_user_config.get(bob) == {"debug_mode": False}


def test_get_debug_mode_returns_none_when_unset(pg_env, test_user_id):
    assert get_debug_mode(test_user_id) is None


def test_get_set_debug_mode_roundtrip(pg_env, test_user_id):
    set_debug_mode(test_user_id, True)
    assert get_debug_mode(test_user_id) is True

    set_debug_mode(test_user_id, False)
    assert get_debug_mode(test_user_id) is False
