"""
运行台账（workspace/index.db）。

checkpointer 按 thread_id 存图状态；换会话后问「上次执行怎么样了」
需要能跨会话查找 pipeline，所以单独建表。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from work_agent.core.config import get_settings


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


def _now() -> str:
    """UTC ISO 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


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
            # 旧表 runs / 无 pipeline_id：开发期直接重建
            old_runs = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()
            if old_runs or (cols and "pipeline_id" not in cols):
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
                    updated_at TEXT NOT NULL
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
        """
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pipelines (
                    pipeline_id, task_id, case_names, version, env,
                    status, report_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    updated_at=excluded.updated_at
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
                    status, report_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            conn.commit()

    def get(self, pipeline_id: str) -> PipelineRecord | None:
        """
        按 id 取一条记录。

        参数:
            pipeline_id: 流水线 id。

        返回:
            PipelineRecord 或 None。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pipelines WHERE pipeline_id=?", (pipeline_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def latest(self, limit: int = 1) -> list[PipelineRecord]:
        """
        按创建时间倒序取最近若干条。

        参数:
            limit: 条数上限。

        返回:
            PipelineRecord 列表。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pipelines ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def find_by_case(self, case_name: str, limit: int = 10) -> list[PipelineRecord]:
        """
        查找包含某用例名的流水线（扫最近 100 条）。

        参数:
            case_name: 用例名。
            limit: 最多返回条数。

        返回:
            匹配的 PipelineRecord 列表。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pipelines ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        out: list[PipelineRecord] = []
        for row in rows:
            rec = self._row_to_record(row)
            if case_name in rec.case_names:
                out.append(rec)
            if len(out) >= limit:
                break
        return out

    def find_by_task(self, task_id: str) -> list[PipelineRecord]:
        """
        按 task_id 列出同任务下全部流水线（创建时间升序）。

        参数:
            task_id: 任务 id。

        返回:
            PipelineRecord 列表。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pipelines WHERE task_id=? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_recent(self, limit: int = 10) -> list[PipelineRecord]:
        """
        最近流水线列表（等同 latest）。

        参数:
            limit: 条数上限。

        返回:
            PipelineRecord 列表。
        """
        return self.latest(limit=limit)

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
        )


@lru_cache(maxsize=1)
def get_ledger() -> RunLedger:
    """
    返回进程内共享的 RunLedger（workspace/index.db）。

    返回:
        缓存的 RunLedger 单例。
    """
    return RunLedger(get_settings().workspace_dir / "index.db")
