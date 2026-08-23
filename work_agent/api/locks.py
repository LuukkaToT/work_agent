"""
按 thread_id 加锁：同一个会话不能被两个请求同时并发续跑。

checkpointer 的读-改-写不是为并发设计的：两个请求同时对同一个 thread_id
调用 invoke，谁的 checkpoint 后写谁赢，容易把状态写乱、或者让 interrupt
的语义变得不可预测。这里用最简单的进程内内存锁做非阻塞互斥——抢不到就直接
告诉客户端「稍后重试」，不排队等，避免请求堆在线程池里。

已知局限：这层锁只在单进程内有效。多副本部署（多个网关进程/多台机器）时
需要换成基于 Postgres 的 advisory lock 或分布式锁；当前阶段单进程部署，
这个不是问题，留在阶段2之后按需再做。
"""

from __future__ import annotations

import threading
from collections import defaultdict

_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def try_acquire_thread_lock(thread_id: str) -> threading.Lock | None:
    """
    非阻塞尝试获取某 thread 的锁。

    参数:
        thread_id: 会话 id。

    返回:
        拿到锁则返回锁对象（调用方用完必须 ``release()``）；拿不到返回 None。
    """
    with _locks_guard:
        lock = _locks[thread_id]
    return lock if lock.acquire(blocking=False) else None
