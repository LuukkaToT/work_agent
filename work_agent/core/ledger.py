"""
运行台账：跨会话查找流水线（"上次执行怎么样了"）。

checkpointer 按 thread_id 存图状态；换会话后问「上次执行怎么样了」
需要能跨会话查找 pipeline，所以单独建表。

后端选择：``POSTGRES_DSN`` 配了用 ``PostgresLedger``（上线，多进程共享，走
core/db.py 的连接池）；没配用 ``RunLedger``（本地 SQLite，workspace/index.db）。
两者实现同一套方法签名（见 ``LedgerProtocol``），调用方（exec_flow /
pipeline_ops / diagnose_tools / pipeline_resolve）只认协议，不关心具体后端。

多用户隔离：``pipelines.user_id`` 存创建者工号（如 z00888363），不是未来
用户表的 int 主键——工号是从鉴权拿到的稳定业务身份，台账没必要为了一个代理键去
反查一张（本期还不存在的）用户表。``user_id`` 目前是可选参数（默认空串），等
identity.py 把真实身份接进 state 后（阶段3），调用方再传真实值；查询方法已经
支持按 user_id 过滤，接线时只需传参，不需要再改这个文件。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from work_agent.core.config import get_settings
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
    # 创建者工号（如 z00888363）；单用户 CLI 时期允许为空串，多用户接线见 identity.py
    user_id: str = ""


def _now() -> str:
    """UTC ISO 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


class LedgerProtocol(Protocol):
    """台账后端协议：``RunLedger``（SQLite）与 ``PostgresLedger`` 都实现这套方法。"""

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


class RunLedger:
    """SQLite 流水线台账：跨会话查找 / 更新状态。"""

    def __init__(self, db_path: Path) -> None:
        """
        参数:
            db_path: index.db 路径；父目录不存在会自动创建。
        """
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """打开带 Row factory 的连接。"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """建表；开发期遇旧 schema 则重建。"""
        with self._connect() as conn:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(pipelines)").fetchall()
            }
            # 旧表 runs / 缺 pipeline_id / 缺 user_id：开发期直接重建
            old_runs = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()
            missing_cols = cols and ("pipeline_id" not in cols or "user_id" not in cols)
            if old_runs or missing_cols:
                conn.execute("DROP TABLE IF EXISTS runs")
                conn.execute("DROP TABLE IF EXISTS pipelines")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pipelines (
                    pipeline_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    case_names TEXT NOT NULL,
                    version TEXT NOT NULL,
                    env TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

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
        """
        插入或覆盖一条流水线记录。

        参数:
            pipeline_id: 主键（可为临时 local- id）。
            task_id: 所属任务 id。
            case_names: 用例名列表。
            version: 软件版本。
            env: 组网 IP。
            status: 状态字符串。
            report_path: 报告路径；空串冲突时不覆盖已有路径。
            user_id: 创建者工号；空串表示未接身份（单用户 CLI 时期）。
        """
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pipelines (
                    pipeline_id, task_id, case_names, version, env,
                    status, report_path, created_at, updated_at, user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pipeline_id) DO UPDATE SET
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
            conn.commit()

    def update_status(
        self,
        pipeline_id: str,
        *,
        status: str | None = None,
        report_path: str | None = None,
    ) -> None:
        """
        更新状态和/或报告路径。

        参数:
            pipeline_id: 目标流水线。
            status: 新状态；None 表示不改。
            report_path: 新路径；None 表示不改。
        """
        fields: list[str] = ["updated_at=?"]
        values: list[str] = [_now()]
        if status is not None:
            fields.append("status=?")
            values.append(status)
        if report_path is not None:
            fields.append("report_path=?")
            values.append(report_path)
        values.append(pipeline_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE pipelines SET {', '.join(fields)} WHERE pipeline_id=?",
                values,
            )
            conn.commit()

    def replace_id(self, old_id: str, new_id: str, *, status: str) -> None:
        """
        把 creating 临时键换成服务端 pipeline_id。

        参数:
            old_id: 临时 local- id。
            new_id: 服务端返回的真实 id。
            status: 替换后写入的状态（通常 created）。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pipelines WHERE pipeline_id=?", (old_id,)
            ).fetchone()
            if not row:
                return
            now = _now()
            conn.execute("DELETE FROM pipelines WHERE pipeline_id=?", (old_id,))
            conn.execute(
                """
                INSERT INTO pipelines (
                    pipeline_id, task_id, case_names, version, env,
                    status, report_path, created_at, updated_at, user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            conn.commit()

    def get(
        self, pipeline_id: str, *, user_id: str | None = None
    ) -> PipelineRecord | None:
        """
        按 id 取一条记录。

        参数:
            pipeline_id: 流水线 id。
            user_id: 不为 None 时，附加校验记录归属，不属于该用户则视为不存在。

        返回:
            PipelineRecord 或 None。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pipelines WHERE pipeline_id=?", (pipeline_id,)
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
        """
        按创建时间倒序取最近若干条。

        参数:
            limit: 条数上限。
            user_id: 不为 None 时只返回该用户的记录。

        返回:
            PipelineRecord 列表。
        """
        sql = "SELECT * FROM pipelines"
        params: list = []
        if user_id is not None:
            sql += " WHERE user_id=?"
            params.append(user_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def find_by_case(
        self, case_name: str, limit: int = 10, *, user_id: str | None = None
    ) -> list[PipelineRecord]:
        """
        查找包含某用例名的流水线（扫最近 100 条）。

        参数:
            case_name: 用例名。
            limit: 最多返回条数。
            user_id: 不为 None 时只在该用户的记录里找。

        返回:
            匹配的 PipelineRecord 列表。
        """
        sql = "SELECT * FROM pipelines"
        params: list = []
        if user_id is not None:
            sql += " WHERE user_id=?"
            params.append(user_id)
        sql += " ORDER BY created_at DESC LIMIT 100"
        with self._connect() as conn:
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
        """
        按 task_id 列出同任务下全部流水线（创建时间升序）。

        参数:
            task_id: 任务 id。
            user_id: 不为 None 时只返回该用户的记录。

        返回:
            PipelineRecord 列表。
        """
        sql = "SELECT * FROM pipelines WHERE task_id=?"
        params: list = [task_id]
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        sql += " ORDER BY created_at ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_recent(
        self, limit: int = 10, *, user_id: str | None = None
    ) -> list[PipelineRecord]:
        """
        最近流水线列表（等同 latest）。

        参数:
            limit: 条数上限。
            user_id: 不为 None 时只返回该用户的记录。

        返回:
            PipelineRecord 列表。
        """
        return self.latest(limit=limit, user_id=user_id)

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> PipelineRecord:
        """sqlite Row → PipelineRecord。"""
        return PipelineRecord(
            pipeline_id=row["pipeline_id"],
            task_id=row["task_id"],
            case_names=json.loads(row["case_names"] or "[]"),
            version=row["version"],
            env=row["env"],
            status=row["status"],
            report_path=row["report_path"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            user_id=row["user_id"] or "",
        )


class PostgresLedger:
    """Postgres 台账实现：上线用，方法签名与 ``RunLedger`` 完全对齐（见 ``LedgerProtocol``）。"""

    def __init__(self) -> None:
        self._ensure_table()

    def _ensure_table(self) -> None:
        """建表（若不存在）；created_at/updated_at 仍存 ISO 字符串，和 SQLite 版一致，
        避免两个后端之间的时间格式/时区处理产生分歧。"""
        with get_pool().connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pipelines (
                    pipeline_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    case_names TEXT NOT NULL,
                    version TEXT NOT NULL,
                    env TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT ''
                )
                """
            )

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
        """插入或覆盖一条流水线记录（语义同 ``RunLedger.upsert``）。"""
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
        """更新状态和/或报告路径（语义同 ``RunLedger.update_status``）。"""
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
        """把 creating 临时键换成服务端 pipeline_id（语义同 ``RunLedger.replace_id``）。"""
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
        """查找包含某用例名的流水线（扫最近 100 条，语义同 SQLite 版）。"""
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
    返回进程内共享的台账实例；按 ``POSTGRES_DSN`` 是否配置自动选后端。

    返回:
        ``PostgresLedger``（配了 DSN）或 ``RunLedger``（本地 SQLite）单例。
    """
    if get_settings().postgres_dsn:
        return PostgresLedger()
    return RunLedger(get_settings().workspace_dir / "index.db")
