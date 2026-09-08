"""Postgres transcript：独立测试库中的隔离、重建回读、过期与失败。"""

from __future__ import annotations

import time
import uuid

import pytest

from work_agent.core.transcript import (
    PostgresTranscriptStore,
    Transcript,
    TranscriptConflict,
    TranscriptError,
    TranscriptExpired,
    TranscriptScope,
)


def _scope() -> TranscriptScope:
    return TranscriptScope(
        user_id="pg-user",
        task_id="pg-task",
        agent_id="diagnose",
        execution_id=uuid.uuid4().hex,
    )


def _cleanup(pool, stream_id: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM agent_transcript_streams WHERE stream_id=%s", (stream_id,)
        )


@pytest.fixture
def pg_transcript(pg_pool):
    scope = _scope()
    store = PostgresTranscriptStore(pool=pg_pool)
    transcript = Transcript(store, scope)
    yield transcript
    _cleanup(pg_pool, scope.stream_id)


def test_postgres_roundtrip_rebuild_and_isolation(pg_pool, pg_transcript):
    tr = pg_transcript
    tr.record("hello", "message", {"text": "AbC\n完整正文", "password": "口令"})
    ref = tr.put_text("跨进程原文")
    stored = tr.get("hello")
    assert stored.payload["text"] == "AbC\n完整正文"
    assert stored.payload["password"] == "[已脱敏]"

    rebuilt = Transcript(PostgresTranscriptStore(pool=pg_pool), tr.scope)
    assert rebuilt.get("hello").payload["text"] == "AbC\n完整正文"
    assert rebuilt.read(ref.artifact_id) == "跨进程原文"

    other = Transcript(
        PostgresTranscriptStore(pool=pg_pool),
        TranscriptScope("pg-user", "pg-task", "other-agent", tr.scope.execution_id),
    )
    try:
        assert other.get("hello") is None
        with pytest.raises(FileNotFoundError):
            other.read(ref.artifact_id)
    finally:
        _cleanup(pg_pool, other.scope.stream_id)


def test_postgres_idempotent_and_conflict(pg_transcript):
    tr = pg_transcript
    tr.record("e1", "message", {"n": 1})
    again = tr.record("e1", "message", {"n": 1})
    assert again.seq == 1
    with pytest.raises(TranscriptConflict):
        tr.record("e1", "message", {"n": 2})
    page = tr.events(after_seq=0, limit=10)
    assert [event.event_id for event in page] == ["e1"]


def test_postgres_expired_stream_stays_readable_as_expired(pg_pool):
    scope = _scope()
    store = PostgresTranscriptStore(pool=pg_pool, retention_days=1 / 864000)
    tr = Transcript(store, scope)
    try:
        tr.record("old", "message", {"n": 1})
        time.sleep(0.2)
        with pytest.raises(TranscriptExpired):
            Transcript(store, scope).get("old")
        with pytest.raises(TranscriptExpired):
            Transcript(store, scope).record("new", "message", {"n": 2})
        with pg_pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM agent_transcript_events WHERE stream_id=%s AND event_id=%s",
                (scope.stream_id, "old"),
            ).fetchone()
        assert row is not None
    finally:
        _cleanup(pg_pool, scope.stream_id)


def test_postgres_storage_failure_is_transcript_error():
    class _DeadPool:
        def connection(self):
            raise OSError("pool closed")

    store = PostgresTranscriptStore(pool=_DeadPool())
    tr = Transcript(store, _scope())
    with pytest.raises(TranscriptError, match="记录存储操作失败"):
        tr.record("later", "message", {"n": 1})
