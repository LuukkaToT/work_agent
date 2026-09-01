"""进程与依赖探活；给 K8s / 负载均衡用，不走用户鉴权。"""

from __future__ import annotations

from work_agent.core import db as db_mod


def postgres_ready() -> tuple[bool, str]:
    """
    探测生产 DSN 能否 ``SELECT 1``。

    返回:
        ``(通过, 原因码)``；原因码给日志用，不要把 DSN 或异常原文回给客户端。
    """
    dsn = ""
    try:
        dsn = (db_mod.get_settings().postgres_dsn or "").strip()
    except Exception:  # noqa: BLE001
        return False, "settings"
    if not dsn:
        return False, "no_dsn"
    try:
        pool = db_mod.get_pool()
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return True, "ok"
    except Exception:  # noqa: BLE001
        return False, "unreachable"
