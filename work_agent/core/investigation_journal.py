"""诊断调查的 Durable control plane：租约、fencing、结果引用。

任务表只回答谁该跑、跑到哪；报告与证据原文在 Transcript 的 result_ref。
同一 investigation_id 可多次 attempt，每次必须换新 execution_id，禁止在旧流上续 loop/start。
finish 用 owner_token + RUNNING 做 CAS：0 行表示租约已被接管，晚到结果丢弃。

MemoryInvestigationJournal 给单测与无 DSN 调用。若诊断注入的是 MemoryTranscriptStore，
即使环境有 POSTGRES_DSN 也必须用 Memory journal，避免本机单测打到未迁表的库。
"""

from __future__ import annotations

import copy
import json
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


class InvestigationConflict(InvestigationJournalError):
    """同一 ID 已绑定不同 intent，不能静默当成幂等重放。"""

    def __init__(self, code: str, message: str) -> None:
        """
        参数:
            code: 稳定错误码，如 ``run_id_conflict`` / ``investigation_id_conflict``。
            message: 给人看的说明。
        """
        super().__init__(message)
        self.code = code


@dataclass
class DiagnosisRunRecord:
    """一次诊断编排的控制面记录：身份、预算、当前轮与编排结果引用。"""

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
    """一条组件调查任务：租约、attempt、execution_id 与结果引用。"""

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
    """当前 Unix 时间戳（秒），给租约与内存实现用。"""
    return time.time()


def _ts(value: Any) -> float:
    """
    把库里的时间列收成 float 秒。

    参数:
        value: ``None``、datetime 或可转 float 的值。

    返回:
        Unix 秒；空值记 0。
    """
    if value is None:
        return 0.0
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    return float(value or 0)


def _scope_json(value: Mapping[str, Any] | dict) -> dict:
    """
    把 log_scope 收成可落库的 dict。

    参数:
        value: 普通 mapping 或带 ``model_dump`` 的模型。

    返回:
        JSON 友好的 dict 副本。
    """
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value)


def _canonical(value: Any) -> str:
    """稳定 JSON 串，用来比对两次调查载荷是否同一份。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _check_run_identity(
    rec: DiagnosisRunRecord,
    *,
    user_id: str,
    pipeline_id: str,
    max_rounds: int,
    max_tool_calls: int,
) -> None:
    """
    同一 ``run_id`` 必须绑同一用户、流水线与预算，否则拒绝当幂等重放。

    异常:
        InvestigationConflict: 身份字段对不上。
    """
    if (
        rec.user_id != user_id
        or rec.pipeline_id != pipeline_id
        or int(rec.max_rounds) != int(max_rounds)
        or int(rec.max_tool_calls) != int(max_tool_calls)
    ):
        raise InvestigationConflict("run_id_conflict", "run_id 已绑定不同的诊断身份")


def _check_task_identity(
    rec: InvestigationTaskRecord,
    *,
    run_id: str,
    round_index: int,
    component: str,
    question: str,
    log_scope: Mapping[str, Any],
) -> None:
    """
    同一 ``investigation_id`` 必须绑同一调查载荷，否则拒绝当幂等重放。

    异常:
        InvestigationConflict: 任务字段或 log_scope 对不上。
    """
    incoming = _scope_json(log_scope)
    if (
        rec.run_id != run_id
        or int(rec.round_index) != int(round_index)
        or rec.component != component
        or rec.question != question
        or _canonical(rec.log_scope) != _canonical(incoming)
    ):
        raise InvestigationConflict("investigation_id_conflict", "investigation_id 已绑定不同的调查载荷")


class InvestigationJournal:
    """存储实现提供原子编辑；无网络 I/O 进入事务回调之外。"""

    def get_run(self, run_id: str) -> DiagnosisRunRecord | None:
        """
        按 id 读诊断编排记录。

        参数:
            run_id: 诊断 run 主键。

        返回:
            记录副本；不存在则 ``None``。
        """
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
        """
        没有则插入一条 running 记录；已有则校验身份后原样返回。

        参数:
            run_id: 诊断 run 主键。
            user_id: 工号。
            pipeline_id: 被诊断的流水线。
            max_rounds: 编排轮次上限。
            max_tool_calls: 本 run 工具调用预算。

        返回:
            当前记录。

        异常:
            InvestigationConflict: 同一 run_id 已绑不同身份。
        """
        raise NotImplementedError

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        current_round: int | None = None,
        orchestrator_ref: str | None = None,
    ) -> DiagnosisRunRecord:
        """
        更新编排状态；``None`` 的字段保持不变。

        参数:
            run_id: 诊断 run 主键。
            status: 新状态；None 不改。
            current_round: 当前轮次；None 不改。
            orchestrator_ref: 编排结果在 Transcript 里的引用；None 不改。

        返回:
            更新后的记录。

        异常:
            InvestigationJournalError: 记录不存在或写入失败。
        """
        raise NotImplementedError

    def add_used_tool_calls(self, run_id: str, tool_calls: int) -> int:
        """
        累加本 run 已用的工具次数。

        参数:
            run_id: 诊断 run 主键。
            tool_calls: 本次增加量；负数按 0。

        返回:
            累加后的 ``used_tool_calls``。
        """
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
        """
        登记一条 PENDING 调查；已存在则校验载荷后返回。

        参数:
            investigation_id: 调查任务主键。
            run_id: 所属诊断 run。
            round_index: 编排轮次。
            component: 锁定的组件 id。
            question: 本调查要回答的问题。
            log_scope: 日志范围（组件、pipeline 等）。

        返回:
            当前任务记录。

        异常:
            InvestigationConflict: 同一 id 已绑不同载荷。
        """
        raise NotImplementedError

    def get_task(self, investigation_id: str) -> InvestigationTaskRecord | None:
        """
        按 id 读调查任务。

        参数:
            investigation_id: 调查任务主键。

        返回:
            记录副本；不存在则 ``None``。
        """
        raise NotImplementedError

    def list_tasks(self, run_id: str) -> list[InvestigationTaskRecord]:
        """
        列出某次诊断下的全部调查任务，按轮次与创建时间排序。

        参数:
            run_id: 诊断 run 主键。

        返回:
            任务记录列表（深拷贝语义由实现保证）。
        """
        raise NotImplementedError

    def expire_stale(self, run_id: str, *, now: float | None = None) -> list[str]:
        """
        把租约已过期仍标 RUNNING 的任务改成 EXPIRED。

        参数:
            run_id: 只处理该诊断下的任务。
            now: 比较用的时间戳；None 用当前时间。

        返回:
            被过期的 ``investigation_id`` 列表。
        """
        raise NotImplementedError

    def claim(
        self,
        investigation_id: str,
        *,
        timeout_seconds: float,
        now: float | None = None,
    ) -> InvestigationTaskRecord:
        """
        领取任务：换新 owner_token 与 execution_id，禁止在旧流上续跑。

        已 SUCCEEDED 则原样返回（幂等）。RUNNING 且租约未过期则拒绝。

        参数:
            investigation_id: 调查任务主键。
            timeout_seconds: 工作超时；租约会再加缓冲，最短 60 秒。
            now: 比较用的时间戳；None 用当前时间。

        返回:
            RUNNING 记录，带本 attempt 的 token 与 execution_id。

        异常:
            InvestigationInProgress: 仍被有效租约占用。
            InvestigationJournalError: 不存在或状态不能领取。
        """
        raise NotImplementedError

    def finish_succeeded(
        self,
        investigation_id: str,
        owner_token: str,
        result_ref: str,
        *,
        tool_calls: int = 0,
    ) -> bool:
        """
        CAS 完成：仅当 token 匹配且仍是 RUNNING 才写成 SUCCEEDED。

        参数:
            investigation_id: 调查任务主键。
            owner_token: claim 时发的令牌。
            result_ref: Transcript 里报告/证据的引用。
            tool_calls: 本 attempt 工具次数；控制面可不记账，由调用方决定。

        返回:
            ``True`` 表示本进程赢了 CAS；``False`` 表示租约已被接管，晚到结果应丢弃。
        """
        raise NotImplementedError

    def finish_failed(self, investigation_id: str, owner_token: str, error_code: str) -> bool:
        """
        CAS 失败收尾：token 匹配且 RUNNING 才写成 FAILED。

        参数:
            investigation_id: 调查任务主键。
            owner_token: claim 时发的令牌。
            error_code: 稳定失败码，写入 ``error_code`` 列。

        返回:
            是否成功收尾；``False`` 表示租约已易主。
        """
        raise NotImplementedError

    def expire_owned(self, investigation_id: str, owner_token: str) -> bool:
        """
        持有人主动放弃租约（超时取消），写成 EXPIRED。

        参数:
            investigation_id: 调查任务主键。
            owner_token: claim 时发的令牌。

        返回:
            是否由本 token 过期成功。
        """
        raise NotImplementedError


class MemoryInvestigationJournal(InvestigationJournal):
    """进程内实现：RLock 串行化；读路径返回深拷贝，避免调用方改到内部状态。"""

    def __init__(self) -> None:
        """空台账；仅进程内有效。"""
        self._runs: dict[str, DiagnosisRunRecord] = {}
        self._tasks: dict[str, InvestigationTaskRecord] = {}
        self._lock = threading.RLock()

    def get_run(self, run_id: str) -> DiagnosisRunRecord | None:
        """语义同 ``InvestigationJournal.get_run``；加锁后返回深拷贝。"""
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
        """语义同 ``InvestigationJournal.ensure_run``。"""
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
            else:
                _check_run_identity(
                    rec,
                    user_id=user_id,
                    pipeline_id=pipeline_id,
                    max_rounds=max_rounds,
                    max_tool_calls=max_tool_calls,
                )
            return copy.deepcopy(rec)

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        current_round: int | None = None,
        orchestrator_ref: str | None = None,
    ) -> DiagnosisRunRecord:
        """语义同 ``InvestigationJournal.update_run``。"""
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
        """语义同 ``InvestigationJournal.add_used_tool_calls``。"""
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
        """语义同 ``InvestigationJournal.persist_pending``。"""
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
            else:
                _check_task_identity(
                    rec,
                    run_id=run_id,
                    round_index=round_index,
                    component=component,
                    question=question,
                    log_scope=log_scope,
                )
            return copy.deepcopy(rec)

    def get_task(self, investigation_id: str) -> InvestigationTaskRecord | None:
        """语义同 ``InvestigationJournal.get_task``；加锁后返回深拷贝。"""
        with self._lock:
            rec = self._tasks.get(investigation_id)
            return copy.deepcopy(rec) if rec is not None else None

    def list_tasks(self, run_id: str) -> list[InvestigationTaskRecord]:
        """语义同 ``InvestigationJournal.list_tasks``。"""
        with self._lock:
            rows = [copy.deepcopy(rec) for rec in self._tasks.values() if rec.run_id == run_id]
        rows.sort(key=lambda item: (item.round_index, item.created_at, item.investigation_id))
        return rows

    def expire_stale(self, run_id: str, *, now: float | None = None) -> list[str]:
        """语义同 ``InvestigationJournal.expire_stale``。"""
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
        """语义同 ``InvestigationJournal.claim``；进程内用同一把 RLock 串行化。"""
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
        """语义同 ``InvestigationJournal.finish_succeeded``。"""
        with self._lock:
            rec = self._tasks.get(investigation_id)
            if rec is None or rec.owner_token != owner_token or rec.status != "RUNNING":
                return False
            rec.status = "SUCCEEDED"
            rec.result_ref = result_ref
            rec.error_code = ""
            rec.lease_until = 0
            rec.updated_at = _now()
            _ = tool_calls
            return True

    def finish_failed(self, investigation_id: str, owner_token: str, error_code: str) -> bool:
        """语义同 ``InvestigationJournal.finish_failed``。"""
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
        """语义同 ``InvestigationJournal.expire_owned``。"""
        with self._lock:
            rec = self._tasks.get(investigation_id)
            if rec is None or rec.owner_token != owner_token or rec.status != "RUNNING":
                return False
            rec.status = "EXPIRED"
            rec.lease_until = 0
            rec.updated_at = _now()
            return True


class PostgresInvestigationJournal(InvestigationJournal):
    """Postgres 实现：事务内改行；claim 用事务级 advisory lock 防并发抢同一任务。"""

    def __init__(self, pool=None) -> None:
        """
        参数:
            pool: 已打开的连接池；None 时首次访问才走项目 ``get_pool()``。
        """
        self._pool = pool

    @property
    def pool(self):
        """显式注入的池或项目共享池；构造对象本身不连库。"""
        return self._pool if self._pool is not None else get_pool()

    @staticmethod
    def _decode_run(row) -> DiagnosisRunRecord | None:
        """把 ``diagnosis_run`` 行收成记录；``None`` 行返回 ``None``。"""
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
        """把 ``investigation_task`` 行收成记录；``log_scope`` 一律收成 dict。"""
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
        """语义同 ``InvestigationJournal.get_run``；库异常收成 ``InvestigationJournalError``。"""
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
        """语义同 ``InvestigationJournal.ensure_run``；``ON CONFLICT DO NOTHING`` 后再读出行校验身份。"""
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
                _check_run_identity(
                    rec,
                    user_id=user_id,
                    pipeline_id=pipeline_id,
                    max_rounds=max_rounds,
                    max_tool_calls=max_tool_calls,
                )
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
        """语义同 ``InvestigationJournal.update_run``。"""
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
        """语义同 ``InvestigationJournal.add_used_tool_calls``；原子 ``RETURNING``。"""
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
        """语义同 ``InvestigationJournal.persist_pending``；冲突后回读并校验载荷。"""
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
                _check_task_identity(
                    rec,
                    run_id=run_id,
                    round_index=round_index,
                    component=component,
                    question=question,
                    log_scope=log_scope,
                )
                return rec
        except InvestigationJournalError:
            raise
        except Exception as exc:
            raise InvestigationJournalError("无法写入 investigation_task") from exc

    def get_task(self, investigation_id: str) -> InvestigationTaskRecord | None:
        """语义同 ``InvestigationJournal.get_task``。"""
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
        """语义同 ``InvestigationJournal.list_tasks``。"""
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
        """语义同 ``InvestigationJournal.expire_stale``。"""
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
        """
        语义同 ``InvestigationJournal.claim``。

        先 ``pg_try_advisory_xact_lock``（按 investigation_id 哈希）再 ``FOR UPDATE``：
        抢不到锁视为仍在被另一 worker 更新，抛 ``InvestigationInProgress``。
        """
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
        """语义同 ``InvestigationJournal.finish_succeeded``；0 行更新表示 CAS 失败。"""
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
                _ = tool_calls
                return True
        except Exception as exc:
            raise InvestigationJournalError("无法完成调查任务") from exc

    def finish_failed(self, investigation_id: str, owner_token: str, error_code: str) -> bool:
        """语义同 ``InvestigationJournal.finish_failed``。"""
        return self._finish_status(investigation_id, owner_token, "FAILED", error_code)

    def expire_owned(self, investigation_id: str, owner_token: str) -> bool:
        """语义同 ``InvestigationJournal.expire_owned``。"""
        return self._finish_status(investigation_id, owner_token, "EXPIRED", "")

    def _finish_status(self, investigation_id: str, owner_token: str, status: str, error_code: str) -> bool:
        """
        CAS 收尾：token 匹配且仍 RUNNING 才改成 ``status``。

        参数:
            investigation_id: 调查任务主键。
            owner_token: claim 时发的令牌。
            status: 目标状态，如 ``FAILED`` / ``EXPIRED``。
            error_code: 写入 ``error_code`` 列；过期时可空。

        返回:
            是否由本 token 更新成功。
        """
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
