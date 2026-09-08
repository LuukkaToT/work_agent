"""诊断调查的 Durable control plane：租约、fencing、结果引用。

任务表只回答谁该跑、跑到哪；报告与证据原文在 Transcript 的 result_ref。
同一 investigation_id 可多次 attempt，每次必须换新 execution_id，禁止在旧流上续 loop/start。
finish 用 owner_token + RUNNING 做 CAS：0 行表示租约已被接管，晚到结果丢弃。

MemoryInvestigationJournal 给单测与无 DSN 调用。若诊断注入的是 MemoryTranscriptStore，
即使环境有 POSTGRES_DSN 也必须用 Memory journal，避免本机单测打到未迁表的库。
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from psycopg.types.json import Jsonb

from work_agent.core.db import get_pool


class InvestigationJournalError(RuntimeError):
    """控制面读写失败或租约冲突，调用方必须显式处理。"""


class InvestigationInProgress(InvestigationJournalError):
    """任务仍被有效租约占用，不能再 claim。"""


@dataclass
class DiagnosisRunRecord:
    run_id: str
    user_id: str
    pipeline_id: str
    status: str = "running"
    current_round: int = 0
    max_rounds: int = 3
    max_tool_calls: int = 24
    used_tool_calls: int = 0
    orchestrator_ref: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class InvestigationTaskRecord:
    investigation_id: str
    run_id: str
    round_index: int
    component: str
    question: str
    log_scope: dict = field(default_factory=dict)
    status: str = "PENDING"
    attempt: int = 0
    owner_token: str = ""
    lease_until: float = 0.0
    execution_id: str = ""
    result_ref: str = ""
    error_code: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


def _now() -> float:
    return time.time()


def _ts(value: Any) -> float:
    if value is None:
        return 0.0
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    return float(value or 0)


def _scope_json(value: Mapping[str, Any] | dict) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value)


class InvestigationJournal:
    """存储实现提供原子编辑；无网络 I/O 进入事务回调之外。"""

    def get_run(self, run_id: str) -> DiagnosisRunRecord | None:
        raise NotImplementedError

    def ensure_run(
        self,
        run_id: str,
        *,
        user_id: str,
        pipeline_id: str,
        max_rounds: int,
        max_tool_calls: int,
    ) -> DiagnosisRunRecord:
        raise NotImplementedError

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        current_round: int | None = None,
        orchestrator_ref: str | None = None,
    ) -> DiagnosisRunRecord:
        raise NotImplementedError

    def add_used_tool_calls(self, run_id: str, tool_calls: int) -> int:
        raise NotImplementedError

    def persist_pending(
        self,
        *,
        investigation_id: str,
        run_id: str,
        round_index: int,
        component: str,
        question: str,
        log_scope: Mapping[str, Any],
    ) -> InvestigationTaskRecord:
        raise NotImplementedError

    def get_task(self, investigation_id: str) -> InvestigationTaskRecord | None:
        raise NotImplementedError

    def list_tasks(self, run_id: str) -> list[InvestigationTaskRecord]:
        raise NotImplementedError

    def expire_stale(self, run_id: str, *, now: float | None = None) -> list[str]:
        raise NotImplementedError

    def claim(self, investigation_id: str, *, timeout_seconds: float, now: float | None = None) -> InvestigationTaskRecord:
        raise NotImplementedError

    def finish_succeeded(
        self,
        investigation_id: str,
        owner_token: str,
        result_ref: str,
        *,
        tool_calls: int = 0,
    ) -> bool:
        raise NotImplementedError

    def finish_failed(self, investigation_id: str, owner_token: str, error_code: str) -> bool:
        raise NotImplementedError

    def expire_owned(self, investigation_id: str, owner_token: str) -> bool:
        raise NotImplementedError


class MemoryInvestigationJournal(InvestigationJournal):
    def __init__(self) -> None:
        self._runs: dict[str, DiagnosisRunRecord] = {}
        self._tasks: dict[str, InvestigationTaskRecord] = {}
        self._lock = threading.RLock()

    def get_run(self, run_id: str) -> DiagnosisRunRecord | None:
        with self._lock:
            rec = self._runs.get(run_id)
            return copy.deepcopy(rec) if rec is not None else None

    def ensure_run(
        self,
        run_id: str,
        *,
        user_id: str,
        pipeline_id: str,
        max_rounds: int,
        max_tool_calls: int,
    ) -> DiagnosisRunRecord:
        stamp = _now()
        with self._lock:
            rec = self._runs.get(run_id)
            if rec is None:
                rec = DiagnosisRunRecord(
                    run_id=run_id,
                    user_id=user_id,
                    pipeline_id=pipeline_id,
                    status="running",
                    max_rounds=max_rounds,
                    max_tool_calls=max_tool_calls,
                    created_at=stamp,
                    updated_at=stamp,
                )
                self._runs[run_id] = rec
            return copy.deepcopy(rec)

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        current_round: int | None = None,
        orchestrator_ref: str | None = None,
    ) -> DiagnosisRunRecord:
        with self._lock:
            rec = self._runs.get(run_id)
            if rec is None:
                raise InvestigationJournalError("diagnosis_run 不存在")
            if status is not None:
                rec.status = status
            if current_round is not None:
                rec.current_round = current_round
            if orchestrator_ref is not None:
                rec.orchestrator_ref = orchestrator_ref
            rec.updated_at = _now()
            return copy.deepcopy(rec)

    def add_used_tool_calls(self, run_id: str, tool_calls: int) -> int:
        with self._lock:
            rec = self._runs.get(run_id)
            if rec is None:
                raise InvestigationJournalError("diagnosis_run 不存在")
            rec.used_tool_calls += max(0, int(tool_calls))
            rec.updated_at = _now()
            return rec.used_tool_calls

    def persist_pending(
        self,
        *,
        investigation_id: str,
        run_id: str,
        round_index: int,
        component: str,
        question: str,
        log_scope: Mapping[str, Any],
    ) -> InvestigationTaskRecord:
        stamp = _now()
        with self._lock:
            if run_id not in self._runs:
                raise InvestigationJournalError("diagnosis_run 不存在")
            rec = self._tasks.get(investigation_id)
            if rec is None:
                rec = InvestigationTaskRecord(
                    investigation_id=investigation_id,
                    run_id=run_id,
                    round_index=round_index,
                    component=component,
                    question=question,
                    log_scope=_scope_json(log_scope),
                    status="PENDING",
                    created_at=stamp,
                    updated_at=stamp,
                )
                self._tasks[investigation_id] = rec
            return copy.deepcopy(rec)

    def get_task(self, investigation_id: str) -> InvestigationTaskRecord | None:
        with self._lock:
            rec = self._tasks.get(investigation_id)
            return copy.deepcopy(rec) if rec is not None else None

    def list_tasks(self, run_id: str) -> list[InvestigationTaskRecord]:
        with self._lock:
            rows = [copy.deepcopy(rec) for rec in self._tasks.values() if rec.run_id == run_id]
        rows.sort(key=lambda item: (item.round_index, item.created_at, item.investigation_id))
        return rows

    def expire_stale(self, run_id: str, *, now: float | None = None) -> list[str]:
        stamp = _now() if now is None else now
        expired: list[str] = []
        with self._lock:
            for rec in self._tasks.values():
                if rec.run_id != run_id or rec.status != "RUNNING":
                    continue
                if rec.lease_until > 0 and rec.lease_until <= stamp:
                    rec.status = "EXPIRED"
                    rec.updated_at = stamp
                    expired.append(rec.investigation_id)
        return expired

    def claim(self, investigation_id: str, *, timeout_seconds: float, now: float | None = None) -> InvestigationTaskRecord:
        stamp = _now() if now is None else now
        with self._lock:
            rec = self._tasks.get(investigation_id)
            if rec is None:
                raise InvestigationJournalError("investigation_task 不存在")
            if rec.status == "RUNNING" and rec.lease_until > stamp:
                raise InvestigationInProgress("调查仍在租约内")
            if rec.status == "RUNNING" and rec.lease_until <= stamp:
                rec.status = "EXPIRED"
            if rec.status == "SUCCEEDED":
                return copy.deepcopy(rec)
            if rec.status not in {"PENDING", "EXPIRED", "FAILED"}:
                raise InvestigationJournalError(f"不能 claim 状态 {rec.status}")
            rec.status = "RUNNING"
            rec.attempt += 1
            rec.owner_token = uuid.uuid4().hex
            rec.lease_until = stamp + max(60.0, float(timeout_seconds) + 30.0)
            rec.execution_id = uuid.uuid4().hex
            rec.result_ref = ""
            rec.error_code = ""
            rec.updated_at = stamp
            return copy.deepcopy(rec)

    def finish_succeeded(
        self,
        investigation_id: str,
        owner_token: str,
        result_ref: str,
        *,
        tool_calls: int = 0,
    ) -> bool:
        with self._lock:
            rec = self._tasks.get(investigation_id)
            if rec is None or rec.owner_token != owner_token or rec.status != "RUNNING":
                return False
            rec.status = "SUCCEEDED"
            rec.result_ref = result_ref
            rec.error_code = ""
            rec.lease_until = 0
            rec.updated_at = _now()
            run = self._runs.get(rec.run_id)
            if run is not None:
                run.used_tool_calls += max(0, int(tool_calls))
                run.updated_at = rec.updated_at
            return True

    def finish_failed(self, investigation_id: str, owner_token: str, error_code: str) -> bool:
        with self._lock:
            rec = self._tasks.get(investigation_id)
            if rec is None or rec.owner_token != owner_token or rec.status != "RUNNING":
                return False
            rec.status = "FAILED"
            rec.error_code = error_code
            rec.lease_until = 0
            rec.updated_at = _now()
            return True

    def expire_owned(self, investigation_id: str, owner_token: str) -> bool:
        with self._lock:
            rec = self._tasks.get(investigation_id)
            if rec is None or rec.owner_token != owner_token or rec.status != "RUNNING":
                return False
            rec.status = "EXPIRED"
            rec.lease_until = 0
            rec.updated_at = _now()
            return True


class PostgresInvestigationJournal(InvestigationJournal):
    def __init__(self, pool=None) -> None:
        self._pool = pool

    @property
    def pool(self):
        return self._pool if self._pool is not None else get_pool()

    @staticmethod
    def _decode_run(row) -> DiagnosisRunRecord | None:
        if row is None:
            return None
        return DiagnosisRunRecord(
            run_id=row["run_id"],
            user_id=row["user_id"] or "",
            pipeline_id=row["pipeline_id"],
            status=row["status"],
            current_round=int(row["current_round"]),
            max_rounds=int(row["max_rounds"]),
            max_tool_calls=int(row["max_tool_calls"]),
            used_tool_calls=int(row["used_tool_calls"]),
            orchestrator_ref=row["orchestrator_ref"] or "",
            created_at=_ts(row["created_at"]),
            updated_at=_ts(row["updated_at"]),
        )

    @staticmethod
    def _decode_task(row) -> InvestigationTaskRecord | None:
        if row is None:
            return None
        scope = row["log_scope"]
        if not isinstance(scope, dict):
            scope = dict(scope or {})
        return InvestigationTaskRecord(
            investigation_id=row["investigation_id"],
            run_id=row["run_id"],
            round_index=int(row["round_index"]),
            component=row["component"],
            question=row["question"],
            log_scope=copy.deepcopy(scope),
            status=row["status"],
            attempt=int(row["attempt"]),
            owner_token=row["owner_token"] or "",
            lease_until=float(row["lease_until"] or 0),
            execution_id=row["execution_id"] or "",
            result_ref=row["result_ref"] or "",
            error_code=row["error_code"] or "",
            created_at=_ts(row["created_at"]),
            updated_at=_ts(row["updated_at"]),
        )

    def get_run(self, run_id: str) -> DiagnosisRunRecord | None:
        try:
            with self.pool.connection() as conn:
                return self._decode_run(
                    conn.execute("SELECT * FROM diagnosis_run WHERE run_id=%s", (run_id,)).fetchone()
                )
        except Exception as exc:
            raise InvestigationJournalError("无法读取 diagnosis_run") from exc

    def ensure_run(
        self,
        run_id: str,
        *,
        user_id: str,
        pipeline_id: str,
        max_rounds: int,
        max_tool_calls: int,
    ) -> DiagnosisRunRecord:
        try:
            with self.pool.connection() as conn, conn.transaction():
                conn.execute(
                    "INSERT INTO diagnosis_run "
                    "(run_id, user_id, pipeline_id, status, max_rounds, max_tool_calls) "
                    "VALUES (%s, %s, %s, 'running', %s, %s) ON CONFLICT (run_id) DO NOTHING",
                    (run_id, user_id, pipeline_id, max_rounds, max_tool_calls),
                )
                row = conn.execute("SELECT * FROM diagnosis_run WHERE run_id=%s", (run_id,)).fetchone()
                rec = self._decode_run(row)
                if rec is None:
                    raise InvestigationJournalError("无法创建 diagnosis_run")
                return rec
        except InvestigationJournalError:
            raise
        except Exception as exc:
            raise InvestigationJournalError("无法写入 diagnosis_run") from exc

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        current_round: int | None = None,
        orchestrator_ref: str | None = None,
    ) -> DiagnosisRunRecord:
        assignments = ["updated_at=now()"]
        values: list[Any] = []
        if status is not None:
            assignments.append("status=%s")
            values.append(status)
        if current_round is not None:
            assignments.append("current_round=%s")
            values.append(current_round)
        if orchestrator_ref is not None:
            assignments.append("orchestrator_ref=%s")
            values.append(orchestrator_ref)
        values.append(run_id)
        try:
            with self.pool.connection() as conn, conn.transaction():
                conn.execute(
                    f"UPDATE diagnosis_run SET {', '.join(assignments)} WHERE run_id=%s",
                    tuple(values),
                )
                rec = self._decode_run(
                    conn.execute("SELECT * FROM diagnosis_run WHERE run_id=%s", (run_id,)).fetchone()
                )
                if rec is None:
                    raise InvestigationJournalError("diagnosis_run 不存在")
                return rec
        except InvestigationJournalError:
            raise
        except Exception as exc:
            raise InvestigationJournalError("无法更新 diagnosis_run") from exc

    def add_used_tool_calls(self, run_id: str, tool_calls: int) -> int:
        try:
            with self.pool.connection() as conn, conn.transaction():
                row = conn.execute(
                    "UPDATE diagnosis_run SET used_tool_calls=used_tool_calls+%s, updated_at=now() "
                    "WHERE run_id=%s RETURNING used_tool_calls",
                    (max(0, int(tool_calls)), run_id),
                ).fetchone()
                if row is None:
                    raise InvestigationJournalError("diagnosis_run 不存在")
                return int(row["used_tool_calls"])
        except InvestigationJournalError:
            raise
        except Exception as exc:
            raise InvestigationJournalError("无法更新 used_tool_calls") from exc

    def persist_pending(
        self,
        *,
        investigation_id: str,
        run_id: str,
        round_index: int,
        component: str,
        question: str,
        log_scope: Mapping[str, Any],
    ) -> InvestigationTaskRecord:
        try:
            with self.pool.connection() as conn, conn.transaction():
                conn.execute(
                    "INSERT INTO investigation_task "
                    "(investigation_id, run_id, round_index, component, question, log_scope, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'PENDING') ON CONFLICT (investigation_id) DO NOTHING",
                    (
                        investigation_id,
                        run_id,
                        round_index,
                        component,
                        question,
                        Jsonb(_scope_json(log_scope)),
                    ),
                )
                rec = self._decode_task(
                    conn.execute(
                        "SELECT * FROM investigation_task WHERE investigation_id=%s",
                        (investigation_id,),
                    ).fetchone()
                )
                if rec is None:
                    raise InvestigationJournalError("无法创建 investigation_task")
                return rec
        except InvestigationJournalError:
            raise
        except Exception as exc:
            raise InvestigationJournalError("无法写入 investigation_task") from exc

    def get_task(self, investigation_id: str) -> InvestigationTaskRecord | None:
        try:
            with self.pool.connection() as conn:
                return self._decode_task(
                    conn.execute(
                        "SELECT * FROM investigation_task WHERE investigation_id=%s",
                        (investigation_id,),
                    ).fetchone()
                )
        except Exception as exc:
            raise InvestigationJournalError("无法读取 investigation_task") from exc

    def list_tasks(self, run_id: str) -> list[InvestigationTaskRecord]:
        try:
            with self.pool.connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM investigation_task WHERE run_id=%s "
                    "ORDER BY round_index, created_at, investigation_id",
                    (run_id,),
                ).fetchall()
            return [self._decode_task(row) for row in rows if row is not None]
        except Exception as exc:
            raise InvestigationJournalError("无法列出 investigation_task") from exc

    def expire_stale(self, run_id: str, *, now: float | None = None) -> list[str]:
        stamp = _now() if now is None else now
        try:
            with self.pool.connection() as conn, conn.transaction():
                rows = conn.execute(
                    "UPDATE investigation_task SET status='EXPIRED', updated_at=now() "
                    "WHERE run_id=%s AND status='RUNNING' AND lease_until>0 AND lease_until<=%s "
                    "RETURNING investigation_id",
                    (run_id, stamp),
                ).fetchall()
            return [row["investigation_id"] for row in rows]
        except Exception as exc:
            raise InvestigationJournalError("无法过期调查任务") from exc

    def claim(self, investigation_id: str, *, timeout_seconds: float, now: float | None = None) -> InvestigationTaskRecord:
        stamp = _now() if now is None else now
        lease_until = stamp + max(60.0, float(timeout_seconds) + 30.0)
        owner = uuid.uuid4().hex
        execution_id = uuid.uuid4().hex
        try:
            with self.pool.connection() as conn, conn.transaction():
                locked = conn.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS locked",
                    ("investigation-task:" + investigation_id,),
                ).fetchone()["locked"]
                if not locked:
                    raise InvestigationInProgress("另一个 worker 正在更新该调查")
                row = conn.execute(
                    "SELECT * FROM investigation_task WHERE investigation_id=%s FOR UPDATE",
                    (investigation_id,),
                ).fetchone()
                rec = self._decode_task(row)
                if rec is None:
                    raise InvestigationJournalError("investigation_task 不存在")
                if rec.status == "RUNNING" and rec.lease_until > stamp:
                    raise InvestigationInProgress("调查仍在租约内")
                if rec.status == "SUCCEEDED":
                    return rec
                if rec.status == "RUNNING" and rec.lease_until <= stamp:
                    rec.status = "EXPIRED"
                if rec.status not in {"PENDING", "EXPIRED", "FAILED"}:
                    raise InvestigationJournalError(f"不能 claim 状态 {rec.status}")
                updated = conn.execute(
                    "UPDATE investigation_task SET status='RUNNING', attempt=attempt+1, "
                    "owner_token=%s, lease_until=%s, execution_id=%s, result_ref='', error_code='', "
                    "updated_at=now() WHERE investigation_id=%s RETURNING *",
                    (owner, lease_until, execution_id, investigation_id),
                ).fetchone()
                claimed = self._decode_task(updated)
                if claimed is None:
                    raise InvestigationJournalError("claim 失败")
                return claimed
        except (InvestigationJournalError, InvestigationInProgress):
            raise
        except Exception as exc:
            raise InvestigationJournalError("无法 claim 调查任务") from exc

    def finish_succeeded(
        self,
        investigation_id: str,
        owner_token: str,
        result_ref: str,
        *,
        tool_calls: int = 0,
    ) -> bool:
        try:
            with self.pool.connection() as conn, conn.transaction():
                row = conn.execute(
                    "UPDATE investigation_task SET status='SUCCEEDED', result_ref=%s, error_code='', "
                    "lease_until=0, updated_at=now() "
                    "WHERE investigation_id=%s AND owner_token=%s AND status='RUNNING' "
                    "RETURNING run_id",
                    (result_ref, investigation_id, owner_token),
                ).fetchone()
                if row is None:
                    return False
                if tool_calls:
                    conn.execute(
                        "UPDATE diagnosis_run SET used_tool_calls=used_tool_calls+%s, updated_at=now() "
                        "WHERE run_id=%s",
                        (max(0, int(tool_calls)), row["run_id"]),
                    )
                return True
        except Exception as exc:
            raise InvestigationJournalError("无法完成调查任务") from exc

    def finish_failed(self, investigation_id: str, owner_token: str, error_code: str) -> bool:
        return self._finish_status(investigation_id, owner_token, "FAILED", error_code)

    def expire_owned(self, investigation_id: str, owner_token: str) -> bool:
        return self._finish_status(investigation_id, owner_token, "EXPIRED", "")

    def _finish_status(self, investigation_id: str, owner_token: str, status: str, error_code: str) -> bool:
        try:
            with self.pool.connection() as conn, conn.transaction():
                row = conn.execute(
                    "UPDATE investigation_task SET status=%s, error_code=%s, lease_until=0, updated_at=now() "
                    "WHERE investigation_id=%s AND owner_token=%s AND status='RUNNING' "
                    "RETURNING investigation_id",
                    (status, error_code, investigation_id, owner_token),
                ).fetchone()
                return row is not None
        except Exception as exc:
            raise InvestigationJournalError("无法更新调查任务状态") from exc
