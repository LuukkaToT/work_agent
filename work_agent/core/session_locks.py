"""会话级进程内互斥；REST 与 MCP 入口共同使用。"""

from __future__ import annotations

import threading
from collections import defaultdict

_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def try_acquire_thread_lock(thread_id: str) -> threading.Lock | None:
    """非阻塞获取 thread 锁；调用方成功后必须负责 ``release()``。"""
    with _locks_guard:
        lock = _locks[thread_id]
    return lock if lock.acquire(blocking=False) else None
