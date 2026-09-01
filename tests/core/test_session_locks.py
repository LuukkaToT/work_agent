"""thread 锁：无 DSN 走内存；有测试库时跨连接 advisory lock 互斥。"""

from work_agent.core.session_locks import try_acquire_thread_lock


def test_memory_lock_rejects_second_acquire(monkeypatch):
    monkeypatch.setattr(
        "work_agent.core.session_locks._postgres_dsn", lambda: ""
    )
    tid = "z00888363-memlock"
    first = try_acquire_thread_lock(tid)
    assert first is not None
    try:
        assert try_acquire_thread_lock(tid) is None
    finally:
        first.release()
    again = try_acquire_thread_lock(tid)
    assert again is not None
    again.release()


def test_postgres_advisory_lock_is_exclusive_across_connections(pg_env, pg_pool):
    tid = "z00888363-pg-advisory"
    first = try_acquire_thread_lock(tid)
    assert first is not None
    try:
        assert try_acquire_thread_lock(tid) is None
    finally:
        first.release()
    second = try_acquire_thread_lock(tid)
    assert second is not None
    second.release()
