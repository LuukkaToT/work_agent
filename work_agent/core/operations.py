"""Durable write-ahead journal with bounded, fenced operation attempts.

No network I/O occurs inside a journal transaction. A crash after claiming an
attempt is an uncertain write, even when it happened just before the send.
MemoryOperationJournal is explicitly for direct/offline callers and tests.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Callable

from psycopg.types.json import Jsonb

from work_agent.core.db import get_pool
from work_agent.core.execution_errors import ExecutionBlocked, ExecutionRetryable


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def require_key(key: str) -> str:
    if not isinstance(key, str) or not key.strip() or key != key.strip():
        raise ExecutionBlocked("idempotency_key_required", "A nonempty idempotency_key is required")
    return key


def operation_key(user_id: str, task_id: str, plan_index: int, operation: str, pipeline_id: str = "") -> str:
    """Hash unambiguous fields, retaining every character of the task UUID."""
    if not task_id or plan_index < 0 or operation not in {"create", "start"}:
        raise ExecutionBlocked("invalid_operation_identity", "Stable task_id, plan index and operation are required")
    if operation == "start" and not pipeline_id:
        raise ExecutionBlocked("invalid_operation_identity", "A start operation requires its target pipeline_id")
    digest = hashlib.sha256(canonical([user_id, task_id, plan_index, operation, pipeline_id]).encode()).hexdigest()
    return f"pipeline:{operation}:{digest}"


@dataclass
class Operation:
    idempotency_key: str
    intent: dict
    request: dict
    status: str = "prepared"
    attempts: int = 0
    owner: str = ""
    lease_until: float = 0
    result: Any = None
    error_code: str = ""
    error_message: str = ""


class OperationJournal:
    """Storage implementations provide atomic edit and independent read."""

    def get(self, key: str) -> Operation | None:
        raise NotImplementedError

    def prepare(self, key: str, intent: dict, freeze: Callable[[], dict]) -> Operation:
        require_key(key)
        with self.edit(key) as slot:
            if slot[0] is None:
                # JSON roundtrip detaches caller-owned mutable values and validates
                # that exactly the same representation can be persisted in PG.
                slot[0] = Operation(key, json.loads(canonical(intent)), json.loads(canonical(freeze())))
            elif canonical(slot[0].intent) != canonical(intent):
                raise ExecutionBlocked("idempotency_conflict", "The idempotency key already belongs to a different payload")
            return copy.deepcopy(slot[0])

    def claim(self, key: str, *, retry_supported: bool) -> Operation:
        with self.edit(key) as slot:
            op = slot[0]
            if op is None:
                raise ExecutionBlocked("operation_missing", "Operation was not prepared")
            if op.status == "succeeded":
                return copy.deepcopy(op)
            if op.status == "blocked":
                raise ExecutionBlocked(op.error_code, op.error_message)
            if op.status == "in_progress" and op.lease_until > time.time():
                raise ExecutionRetryable("operation_in_progress", "The operation is still in progress; retry after its lease expires")
            if op.attempts and not (retry_supported and op.request["idempotency_supported"]):
                raise ExecutionBlocked("operation_uncertain", "Write outcome is uncertain and backend idempotency is disabled; reconcile before continuing")
            if op.attempts >= min(3, max(1, op.request.get("max_attempts", 3))):
                raise ExecutionBlocked("operation_retry_exhausted", "Operation retry budget exhausted; reconcile the backend result")
            op.status = "in_progress"
            op.attempts += 1
            op.owner = uuid.uuid4().hex
            op.lease_until = time.time() + max(60.0, float(op.request.get("timeout", 60)) + 30)
            return copy.deepcopy(op)

    def finish(self, op: Operation, *, result: Any = None, error: Exception | None = None) -> None:
        with self.edit(op.idempotency_key) as slot:
            current = slot[0]
            if current is None or current.owner != op.owner or current.status != "in_progress":
                raise ExecutionRetryable("operation_lease_lost", "Another attempt owns this operation; resume from the journal")
            if error is None:
                current.status = "succeeded"
                current.result = result
            else:
                current.status = "blocked" if isinstance(error, ExecutionBlocked) else "retryable"
                current.error_code = getattr(error, "code", "operation_uncertain")
                current.error_message = str(error)
            current.lease_until = 0


class MemoryOperationJournal(OperationJournal):
    def __init__(self) -> None:
        self._records: dict[str, Operation] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> Operation | None:
        with self._lock:
            return copy.deepcopy(self._records.get(key))

    @contextmanager
    def edit(self, key: str):
        with self._lock:
            slot = [copy.deepcopy(self._records.get(key))]
            yield slot
            if slot[0] is not None:
                self._records[key] = copy.deepcopy(slot[0])


class PostgresOperationJournal(OperationJournal):
    def __init__(self, pool=None) -> None:
        self._pool = pool

    @property
    def pool(self):
        return self._pool if self._pool is not None else get_pool()

    @staticmethod
    def _decode(row) -> Operation | None:
        return Operation(**{name: row[name] for name in Operation.__dataclass_fields__}) if row else None

    def get(self, key: str) -> Operation | None:
        try:
            with self.pool.connection() as conn:
                return self._decode(conn.execute("SELECT * FROM pipeline_operations WHERE idempotency_key=%s", (key,)).fetchone())
        except Exception as exc:
            raise ExecutionRetryable("operation_journal_unavailable", "Cannot read the durable operation journal") from exc

    @contextmanager
    def edit(self, key: str):
        try:
            with self.pool.connection() as conn, conn.transaction():
                locked = conn.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS locked", ("pipeline-operation:" + key,)).fetchone()["locked"]
                if not locked:
                    raise ExecutionRetryable("operation_in_progress", "Another worker is updating this operation")
                row = conn.execute("SELECT * FROM pipeline_operations WHERE idempotency_key=%s FOR UPDATE", (key,)).fetchone()
                slot = [self._decode(row)]
                yield slot
                if slot[0] is not None:
                    values = asdict(slot[0])
                    for field in ("intent", "request", "result"):
                        values[field] = Jsonb(values[field])
                    columns = list(values)
                    updates = ", ".join(f"{name}=excluded.{name}" for name in columns if name != "idempotency_key")
                    conn.execute(
                        f"INSERT INTO pipeline_operations ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) "
                        f"ON CONFLICT (idempotency_key) DO UPDATE SET {updates}, updated_at=now()",
                        tuple(values.values()),
                    )
        except (ExecutionBlocked, ExecutionRetryable):
            raise
        except Exception as exc:
            raise ExecutionRetryable("operation_journal_unavailable", "Cannot commit the durable operation journal") from exc


def execute_operation(journal: OperationJournal, key: str, intent: dict, freeze: Callable[[], dict], send: Callable[[dict], Any], *, retry_supported: bool) -> Any:
    """One attempt per call; the persisted budget also bounds graph/process retries."""
    journal.prepare(key, intent, freeze)
    op = journal.claim(key, retry_supported=retry_supported)
    if op.status == "succeeded":
        return op.result
    try:
        result = send(copy.deepcopy(op.request))
    except Exception as exc:
        if isinstance(exc, (ExecutionBlocked, ExecutionRetryable)):
            outcome = exc
        elif retry_supported and op.request["idempotency_supported"]:
            outcome = ExecutionRetryable("operation_uncertain", "Pipeline write outcome is uncertain; resume with the same key")
        else:
            outcome = ExecutionBlocked("operation_uncertain", "Pipeline write outcome is uncertain and backend idempotency is disabled")
        journal.finish(op, error=outcome)
        raise outcome from exc
    try:
        journal.finish(op, result=result)
    except Exception as exc:
        if not (retry_supported and op.request["idempotency_supported"]):
            raise ExecutionBlocked("operation_uncertain", "Backend accepted the write but saving its result failed; reconcile before continuing") from exc
        raise ExecutionRetryable("operation_journal_unavailable", "Backend accepted the write but saving its result failed; resume with the same key") from exc
    return result
