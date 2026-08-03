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
    return datetime.now(timezone.utc).isoformat()


class RunLedger:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
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
        """creating 临时键换成服务端 pipeline_id。"""
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
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pipelines WHERE pipeline_id=?", (pipeline_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def latest(self, limit: int = 1) -> list[PipelineRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pipelines ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def find_by_case(self, case_name: str, limit: int = 10) -> list[PipelineRecord]:
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
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pipelines WHERE task_id=? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_recent(self, limit: int = 10) -> list[PipelineRecord]:
        return self.latest(limit=limit)

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> PipelineRecord:
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
    return RunLedger(get_settings().workspace_dir / "index.db")
