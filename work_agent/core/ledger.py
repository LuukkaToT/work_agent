"""
运行台账（workspace/index.db）。

checkpointer 按 thread_id 存图状态；换会话后问「上次执行怎么样了」
需要能跨会话查找 run，所以单独建表。
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
class RunRecord:
    run_id: str
    task_id: str
    case_names: list[str]
    version: str
    topology: str
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    case_names TEXT NOT NULL,
                    version TEXT NOT NULL,
                    topology TEXT NOT NULL,
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
        run_id: str,
        task_id: str,
        case_names: list[str],
        version: str,
        topology: str,
        status: str,
        report_path: str = "",
    ) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, task_id, case_names, version, topology,
                    status, report_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    task_id=excluded.task_id,
                    case_names=excluded.case_names,
                    version=excluded.version,
                    topology=excluded.topology,
                    status=excluded.status,
                    report_path=CASE
                        WHEN excluded.report_path != '' THEN excluded.report_path
                        ELSE runs.report_path
                    END,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    task_id,
                    json.dumps(case_names, ensure_ascii=False),
                    version,
                    topology,
                    status,
                    report_path,
                    now,
                    now,
                ),
            )
            conn.commit()

    def update_status(
        self,
        run_id: str,
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
        values.append(run_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE runs SET {', '.join(fields)} WHERE run_id=?",
                values,
            )
            conn.commit()

    def get(self, run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def latest(self, limit: int = 1) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def find_by_case(self, case_name: str, limit: int = 10) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        out: list[RunRecord] = []
        for row in rows:
            rec = self._row_to_record(row)
            if case_name in rec.case_names:
                out.append(rec)
            if len(out) >= limit:
                break
        return out

    def list_recent(self, limit: int = 10) -> list[RunRecord]:
        return self.latest(limit=limit)

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            task_id=row["task_id"],
            case_names=json.loads(row["case_names"] or "[]"),
            version=row["version"],
            topology=row["topology"],
            status=row["status"],
            report_path=row["report_path"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@lru_cache(maxsize=1)
def get_ledger() -> RunLedger:
    return RunLedger(get_settings().workspace_dir / "index.db")
