"""
按 thread_id 加锁：同一个会话不能被两个请求同时并发续跑。

checkpointer 的读-改-写不是为并发设计的：两个请求同时对同一个 thread_id
调用 invoke，谁的 checkpoint 后写谁赢，容易把状态写乱、或者让 interrupt
的语义变得不可预测。抢不到就直接告诉客户端「稍后重试」，不排队等。

实现见 ``work_agent.core.session_locks``：配置了 POSTGRES_DSN 时用
Postgres session advisory lock（跨进程/跨 Pod 有效）；未配 DSN 时退回
进程内内存锁。REST 抢不到返回 409，MCP 返回 THREAD_BUSY。
"""

from work_agent.core.session_locks import try_acquire_thread_lock

__all__ = ["try_acquire_thread_lock"]
