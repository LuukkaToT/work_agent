"""
运行台账：跨会话查找流水线（"上次执行怎么样了"）。

checkpointer 按 thread_id 存图状态；换会话后问「上次执行怎么样了」
需要能跨会话查找 pipeline，所以单独建表。

只走 Postgres（``PostgresLedger``，``core/db.py`` 连接池）。表由
``python -m work_agent init-db`` 创建，本模块运行时不建表。

多用户隔离：``pipelines.user_id`` 存创建者工号（如 z00888363）。
历史空 ``user_id`` 的回填在 ``sql/schema.sql`` 里，不在启动热路径。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Protocol

from work_agent.core.db import get_pool


@dataclass(frozen=True)
class PipelineRecord:
    """台账中的一条流水线记录。"""

    pipeline_id: str
    task_id: str
    case_names: list[str]
    version: str
    env: str
    status: str
    report_path: str
    created_at: str
    updated_at: str
    user_id: str = ""


def _now() -> str:
    """UTC ISO 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


class LedgerProtocol(Protocol):
    """台账后端协议。生产与测试都走 ``PostgresLedger``。"""

    def upsert(
        self,
        *,
        pipeline_id: str,
        task_id: str,
        case_names: list[str],
        version: str,
        env: str,
        status: str,
        report_path: str = "",
        user_id: str = "",
    ) -> None: ...

    def update_status(
        self,
        pipeline_id: str,
        *,
        status: str | None = None,
        report_path: str | None = None,
    ) -> None: ...

    def replace_id(self, old_id: str, new_id: str, *, status: str) -> None: ...

    def get(
        self, pipeline_id: str, *, user_id: str | None = None
    ) -> PipelineRecord | None: ...

    def latest(
        self, limit: int = 1, *, user_id: str | None = None
    ) -> list[PipelineRecord]: ...

    def find_by_case(
        self, case_name: str, limit: int = 10, *, user_id: str | None = None
    ) -> list[PipelineRecord]: ...

    def find_by_task(
        self, task_id: str, *, user_id: str | None = None
    ) -> list[PipelineRecord]: ...

    def list_recent(
        self, limit: int = 10, *, user_id: str | None = None
    ) -> list[PipelineRecord]: ...


class PostgresLedger:
    """Postgres 台账。表须已由 init-db 建好。"""

    def upsert(
        self,
        *,
        pipeline_id: str,
        task_id: str,
        case_names: list[str],
        version: str,
        env: str,
        status: str,
        report_path: str = "",
        user_id: str = "",
    ) -> None:
        """插入或覆盖一条流水线记录。空 report_path 冲突时不覆盖已有路径。"""
        now = _now()
        with get_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO pipelines (
                    pipeline_id, task_id, case_names, version, env,
                    status, report_path, created_at, updated_at, user_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (pipeline_id) DO UPDATE SET
                    task_id=excluded.task_id,
                    case_names=excluded.case_names,
                    version=excluded.version,
                    env=excluded.env,
                    status=excluded.status,
                    report_path=CASE
                        WHEN excluded.report_path != '' THEN excluded.report_path
                        ELSE pipelines.report_path
                    END,
                    updated_at=excluded.updated_at,
                    user_id=excluded.user_id
                """,
                (
                    pipeline_id,
                    task_id,
                    json.dumps(case_names, ensure_ascii=False),
                    version,
                    env,
                    status,
                    report_path,
                    now,
                    now,
                    user_id,
                ),
            )

    def update_status(
        self,
        pipeline_id: str,
        *,
        status: str | None = None,
        report_path: str | None = None,
    ) -> None:
        """更新状态和/或报告路径。"""
        fields: list[str] = ["updated_at=%s"]
        values: list[str] = [_now()]
        if status is not None:
            fields.append("status=%s")
            values.append(status)
        if report_path is not None:
            fields.append("report_path=%s")
            values.append(report_path)
        values.append(pipeline_id)
        with get_pool().connection() as conn:
            conn.execute(
                f"UPDATE pipelines SET {', '.join(fields)} WHERE pipeline_id=%s",
                values,
            )

    def replace_id(self, old_id: str, new_id: str, *, status: str) -> None:
        """把 creating 临时键换成服务端 pipeline_id。"""
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT * FROM pipelines WHERE pipeline_id=%s", (old_id,)
            ).fetchone()
            if not row:
                return
            now = _now()
            conn.execute("DELETE FROM pipelines WHERE pipeline_id=%s", (old_id,))
            conn.execute(
                """
                INSERT INTO pipelines (
                    pipeline_id, task_id, case_names, version, env,
                    status, report_path, created_at, updated_at, user_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    new_id,
                    row["task_id"],
                    row["case_names"],
                    row["version"],
                    row["env"],
                    status,
                    row["report_path"] or "",
                    row["created_at"],
                    now,
                    row["user_id"] or "",
                ),
            )

    def get(
        self, pipeline_id: str, *, user_id: str | None = None
    ) -> PipelineRecord | None:
        """按 id 取一条记录；user_id 不为 None 时附加校验归属。"""
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT * FROM pipelines WHERE pipeline_id=%s", (pipeline_id,)
            ).fetchone()
        if not row:
            return None
        rec = self._row_to_record(row)
        if user_id is not None and rec.user_id != user_id:
            return None
        return rec

    def latest(
        self, limit: int = 1, *, user_id: str | None = None
    ) -> list[PipelineRecord]:
        """按创建时间倒序取最近若干条；user_id 不为 None 时只返回该用户的记录。"""
        sql = "SELECT * FROM pipelines"
        params: list = []
        if user_id is not None:
            sql += " WHERE user_id=%s"
            params.append(user_id)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with get_pool().connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def find_by_case(
        self, case_name: str, limit: int = 10, *, user_id: str | None = None
    ) -> list[PipelineRecord]:
        """查找包含某用例名的流水线（扫最近 100 条）。"""
        sql = "SELECT * FROM pipelines"
        params: list = []
        if user_id is not None:
            sql += " WHERE user_id=%s"
            params.append(user_id)
        sql += " ORDER BY created_at DESC LIMIT 100"
        with get_pool().connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        out: list[PipelineRecord] = []
        for row in rows:
            rec = self._row_to_record(row)
            if case_name in rec.case_names:
                out.append(rec)
            if len(out) >= limit:
                break
        return out

    def find_by_task(
        self, task_id: str, *, user_id: str | None = None
    ) -> list[PipelineRecord]:
        """按 task_id 列出同任务下全部流水线（创建时间升序）。"""
        sql = "SELECT * FROM pipelines WHERE task_id=%s"
        params: list = [task_id]
        if user_id is not None:
            sql += " AND user_id=%s"
            params.append(user_id)
        sql += " ORDER BY created_at ASC"
        with get_pool().connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_recent(
        self, limit: int = 10, *, user_id: str | None = None
    ) -> list[PipelineRecord]:
        """最近流水线列表（等同 latest）。"""
        return self.latest(limit=limit, user_id=user_id)

    @staticmethod
    def _row_to_record(row: dict) -> PipelineRecord:
        """psycopg dict_row → PipelineRecord。"""
        return PipelineRecord(
            pipeline_id=row["pipeline_id"],
            task_id=row["task_id"],
            case_names=json.loads(row["case_names"] or "[]"),
            version=row["version"],
            env=row["env"],
            status=row["status"],
            report_path=row["report_path"] or "",
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            user_id=row["user_id"] or "",
        )


@lru_cache(maxsize=1)
def get_ledger() -> LedgerProtocol:
    """
    返回进程内共享的台账实例（Postgres）。

    返回:
        ``PostgresLedger`` 单例。未配 DSN 时第一次真正查库会由 ``get_pool()`` 报错。
    """
    return PostgresLedger()
