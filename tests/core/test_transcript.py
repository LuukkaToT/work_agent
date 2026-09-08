"""轻量 transcript 内存存储：隔离、幂等、脱敏与过期。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from work_agent.core.transcript import (
    MemoryTranscriptStore,
    Transcript,
    TranscriptConflict,
    TranscriptError,
    TranscriptExpired,
    TranscriptScope,
    redact,
)
from work_agent.graph.helpers.context_selector import ContextItem


def _scope(**overrides: str) -> TranscriptScope:
    data = {
        "user_id": "user-1",
        "task_id": "task-1",
        "agent_id": "diagnose",
        "execution_id": "exec-1",
    }
    data.update(overrides)
    return TranscriptScope(**data)


def _transcript(**overrides: str) -> Transcript:
    return Transcript(MemoryTranscriptStore(), _scope(**overrides))


def test_scope_rejects_blank_and_control_characters():
    with pytest.raises(TranscriptError):
        TranscriptScope(" ", "t", "a", "e")
    with pytest.raises(TranscriptError):
        TranscriptScope("u", "t", "a", "e\n1")


def test_stream_id_is_stable_content_hash():
    left = _scope()
    right = _scope()
    assert left.stream_id == right.stream_id
    assert len(left.stream_id) == 64
    assert _scope(agent_id="other").stream_id != left.stream_id


def test_record_is_idempotent_and_conflicts_on_different_payload():
    tr = _transcript()
    first = tr.record("evt-1", "message", {"text": "AbC\r\n\n  "})
    second = tr.record("evt-1", "message", {"text": "AbC\r\n\n  "})
    assert first.seq == second.seq == 1
    assert second.payload["text"] == "AbC\r\n\n  "
    with pytest.raises(TranscriptConflict):
        tr.record("evt-1", "message", {"text": "other"})
    with pytest.raises(TranscriptConflict):
        tr.record("evt-1", "other_kind", {"text": "AbC\r\n\n  "})
    assert tr.get("evt-1").payload["text"] == "AbC\r\n\n  "


def test_put_text_preserves_case_newlines_and_is_content_addressed():
    tr = _transcript()
    text = "AbC\r\n\n  未裁剪正文"
    first = tr.put_text(text)
    second = tr.put_text(text)
    assert first.artifact_id == second.artifact_id
    assert tr.read(first.artifact_id) == text


def test_put_json_redacts_nested_secrets_without_rewriting_newlines():
    tr = _transcript()
    ref = tr.put_json({"nested": [{"Api_Key": "秘密"}], "text": "a\r\nb"})
    loaded = tr.read_json(ref.artifact_id)
    assert loaded["nested"][0]["Api_Key"] == "[已脱敏]"
    assert loaded["text"] == "a\r\nb"


def test_event_redaction_uses_original_digest_for_conflict():
    tr = _transcript()
    tr.record("login", "message", {"password": "alpha", "ok": True})
    stored = tr.get("login")
    assert stored.payload["password"] == "[已脱敏]"
    assert stored.payload["ok"] is True
    with pytest.raises(TranscriptConflict):
        tr.record("login", "message", {"password": "beta", "ok": True})


def test_scopes_are_fully_isolated():
    store = MemoryTranscriptStore()
    left = Transcript(store, _scope(user_id="u-left", execution_id="e-left"))
    right = Transcript(store, _scope(user_id="u-right", execution_id="e-right"))
    left.record("shared", "message", {"owner": "left"})
    ref = left.put_text("only-left")
    assert right.get("shared") is None
    with pytest.raises(FileNotFoundError):
        right.read(ref.artifact_id)
    assert left.get("shared").payload["owner"] == "left"


def test_list_events_paginates_by_seq():
    tr = _transcript()
    for index in range(1, 6):
        tr.record(f"e{index}", "message", {"n": index})
    page = tr.events(after_seq=2, limit=2)
    assert [event.seq for event in page] == [3, 4]
    assert [event.event_id for event in page] == ["e3", "e4"]


def test_rebuild_on_same_store_can_reread():
    store = MemoryTranscriptStore()
    scope = _scope()
    first = Transcript(store, scope)
    first.record("hello", "message", {"v": 1})
    ref = first.put_text("正文")
    second = Transcript(store, scope)
    assert second.get("hello").payload["v"] == 1
    assert second.read(ref.artifact_id) == "正文"
    assert second.refs


def test_archive_protocol_roundtrip():
    tr = _transcript()
    item = ContextItem(
        item_id="obs-1",
        kind="tool_result",
        source="fetch_logs",
        text="原始日志",
        priority=4,
    )
    ref = tr.store(item)
    assert tr.read(ref.artifact_id) == "原始日志"
    assert tr.store(item).artifact_id == ref.artifact_id


def test_redact_helper_is_recursive_and_preserves_business_text():
    out = redact({"Token": "abc", "note": "Bearer xyz.abc", "keep": "AbC"})
    assert out["Token"] == "[已脱敏]"
    assert "[已脱敏]" in out["note"]
    assert out["keep"] == "AbC"


def test_custom_redactor_failure_does_not_write():
    def boom(value):
        raise RuntimeError("业务脱敏失败")

    tr = Transcript(MemoryTranscriptStore(), _scope(), redactor=boom)
    with pytest.raises(TranscriptError, match="业务脱敏失败"):
        tr.record("x", "message", {"ok": True})
    assert tr.get("x") is None


def test_expired_stream_rejects_read_and_write():
    store = MemoryTranscriptStore(retention_days=1 / 8640000)
    tr = Transcript(store, _scope(execution_id="expire"))
    tr.record("old", "message", {"n": 1})
    time.sleep(0.05)
    with pytest.raises(TranscriptExpired):
        tr.get("old")
    with pytest.raises(TranscriptExpired):
        tr.record("new", "message", {"n": 2})


def test_concurrent_distinct_events_get_unique_seq():
    store = MemoryTranscriptStore()
    scope = _scope(execution_id="concurrent")
    tr = Transcript(store, scope)

    def write(index: int) -> int:
        return tr.record(f"e{index}", "message", {"n": index}).seq

    with ThreadPoolExecutor(max_workers=8) as pool:
        seqs = list(pool.map(write, range(40)))
    assert sorted(seqs) == list(range(1, 41))
    assert [event.seq for event in tr.events(limit=100)] == list(range(1, 41))


def test_concurrent_same_event_is_idempotent():
    tr = _transcript(execution_id="same-event")
    errors: list[BaseException] = []

    def write() -> None:
        try:
            tr.record("same", "message", {"ok": True})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert tr.get("same").payload == {"ok": True}


def test_concurrent_same_id_different_payload_conflicts():
    tr = _transcript(execution_id="conflict-event")
    results: list[str] = []

    def write(value: str) -> None:
        try:
            tr.record("same", "message", {"v": value})
            results.append("ok:" + value)
        except TranscriptConflict:
            results.append("conflict:" + value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(as_completed([pool.submit(write, "a"), pool.submit(write, "b")]))
    assert any(item.startswith("ok:") for item in results)
    assert any(item.startswith("conflict:") for item in results)
    stored = tr.get("same").payload["v"]
    assert stored in {"a", "b"}


def test_oversized_event_payload_is_rejected():
    tr = _transcript()
    with pytest.raises(TranscriptError, match="65536"):
        tr.record("huge", "message", {"text": "x" * 70_000})
