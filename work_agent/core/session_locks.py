"""会话级互斥：有 Postgres 时用 advisory lock，否则退回进程内锁。

checkpointer 的读-改-写不是为并发设计的，同一个 thread_id 同时 invoke
会把 interrupt 语义写乱。抢不到锁立刻返回 None，不排队。

Postgres 路径必须 checkout 一条连接并一直占着：session 级
``pg_try_advisory_lock`` 绑在后端会话上，还回连接池就等于把锁交给别人。
PgBouncer 事务池会拆掉这条会话，网关需要直连 Postgres 或 session 池。
进程崩溃后后端断开，锁自动释放。
"""

from __future__ import annotations

import hashlib
import threading
from collections import defaultdict
from contextvars import ContextVar
from typing import Protocol

from work_agent.core import db as db_mod

# 与库内其他 advisory 用途隔离。
_LOCK_NAMESPACE = 42
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()
_owned_locks: ContextVar[frozenset[tuple[int, str]]] = ContextVar(
    "owned_thread_locks", default=frozenset()
)


class ThreadLock(Protocol):
    """调用方成功 acquire 后必须 ``release()``。"""

    def release(self) -> None: ...


class _OwnedThreadLock:
    def __init__(self, lock: ThreadLock, thread_id: str) -> None:
        self._lock = lock
        self._released = False
        self._token = _owned_locks.set(
            _owned_locks.get() | {(threading.get_ident(), thread_id)}
        )

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._lock.release()
        finally:
            _owned_locks.reset(self._token)


def owns_thread_lock(thread_id: str) -> bool:
    """True only for the current execution context, including its service wrapper."""
    return (threading.get_ident(), thread_id) in _owned_locks.get()


def is_thread_locked(thread_id: str) -> bool:
    """Probe the same memory/advisory lock used by writers; never runs the graph."""
    if owns_thread_lock(thread_id):
        return True
    lock = try_acquire_thread_lock(thread_id)
    if lock is None:
        return True
    lock.release()
    return False


class _PostgresThreadLock:
    def __init__(self, conn, pool, key: int) -> None:
        self._conn = conn
        self._pool = pool
        self._key = key
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._conn.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (_LOCK_NAMESPACE, self._key),
            )
        finally:
            self._pool.putconn(self._conn)


class _MemoryThreadLock:
    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock

    def release(self) -> None:
        self._lock.release()


def thread_lock_key(thread_id: str) -> int:
    """把 thread_id 映射成 pg_try_advisory_lock 的第二参数（有符号 int32）。"""
    digest = hashlib.sha256(thread_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big", signed=True)


def _postgres_dsn() -> str:
    # 走 db 模块上的 get_settings，pytest pg_env 的 monkeypatch 才能切到测试库。
    return (db_mod.get_settings().postgres_dsn or "").strip()


def try_acquire_thread_lock(thread_id: str) -> ThreadLock | None:
    """非阻塞获取 thread 锁；调用方成功后必须负责 ``release()``。"""
    tid = (thread_id or "").strip()
    if not tid:
        return None
    lock = _pg_try_acquire(tid) if _postgres_dsn() else _memory_try_acquire(tid)
    return _OwnedThreadLock(lock, tid) if lock is not None else None


def _memory_try_acquire(thread_id: str) -> ThreadLock | None:
    with _locks_guard:
        lock = _locks[thread_id]
    if not lock.acquire(blocking=False):
        return None
    return _MemoryThreadLock(lock)


def _pg_try_acquire(thread_id: str) -> ThreadLock | None:
    pool = db_mod.get_pool()
    conn = pool.getconn()
    key = thread_lock_key(thread_id)
    try:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(%s, %s) AS locked",
            (_LOCK_NAMESPACE, key),
        ).fetchone()
        locked = bool(row["locked"] if isinstance(row, dict) else row[0])
        if not locked:
            pool.putconn(conn)
            return None
        return _PostgresThreadLock(conn, pool, key)
    except Exception:
        pool.putconn(conn)
        raise
