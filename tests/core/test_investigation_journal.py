"""Memory InvestigationJournal：claim / fencing / 换 execution_id。"""

from __future__ import annotations

import pytest

from work_agent.core.investigation_journal import (
    InvestigationConflict,
    InvestigationInProgress,
    MemoryInvestigationJournal,
)
from work_agent.core.transcript import (
    MemoryTranscriptStore,
    Transcript,
    TranscriptConflict,
    TranscriptScope,
)
from work_agent.graph.helpers.agent_loop import run_agent_loop
from tests.graph.helpers.test_agent_loop import _ScriptedModel, echo
from langchain_core.messages import AIMessage


def _journal() -> MemoryInvestigationJournal:
    journal = MemoryInvestigationJournal()
    journal.ensure_run(
        "run-1",
        user_id="user-1",
        pipeline_id="p1",
        max_rounds=3,
        max_tool_calls=24,
    )
    journal.persist_pending(
        investigation_id="inv-1",
        run_id="run-1",
        round_index=1,
        component="bbh",
        question="时钟",
        log_scope={"pipeline_id": "p1", "component": "bbh", "tail_lines": 200},
    )
    return journal


def test_claim_finish_and_replay_does_not_new_attempt():
    journal = _journal()
    claimed = journal.claim("inv-1", timeout_seconds=10)
    assert claimed.status == "RUNNING"
    assert claimed.attempt == 1
    assert claimed.execution_id
    journal.add_used_tool_calls("run-1", 2)
    assert journal.finish_succeeded("inv-1", claimed.owner_token, "sha256_" + "a" * 64)
    run = journal.get_run("run-1")
    assert run is not None
    assert run.used_tool_calls == 2
    again = journal.claim("inv-1", timeout_seconds=10)
    assert again.status == "SUCCEEDED"
    assert again.attempt == 1
    assert again.execution_id == claimed.execution_id
    assert again.result_ref.startswith("sha256_")


def test_expired_attempt_gets_new_execution_id():
    journal = _journal()
    first = journal.claim("inv-1", timeout_seconds=10)
    assert journal.expire_owned("inv-1", first.owner_token)
    second = journal.claim("inv-1", timeout_seconds=10)
    assert second.status == "RUNNING"
    assert second.attempt == 2
    assert second.execution_id != first.execution_id
    assert second.owner_token != first.owner_token


def test_late_finish_with_old_owner_is_dropped():
    journal = _journal()
    first = journal.claim("inv-1", timeout_seconds=10)
    assert journal.expire_owned("inv-1", first.owner_token)
    second = journal.claim("inv-1", timeout_seconds=10)
    assert journal.finish_succeeded("inv-1", first.owner_token, "late-ref") is False
    current = journal.get_task("inv-1")
    assert current is not None
    assert current.owner_token == second.owner_token
    assert current.status == "RUNNING"
    assert journal.finish_succeeded("inv-1", second.owner_token, "canonical-ref")
    canonical = journal.get_task("inv-1")
    assert canonical is not None
    assert canonical.result_ref == "canonical-ref"
    assert canonical.status == "SUCCEEDED"


def test_live_lease_rejects_second_claim():
    journal = _journal()
    journal.claim("inv-1", timeout_seconds=10)
    with pytest.raises(InvestigationInProgress):
        journal.claim("inv-1", timeout_seconds=10)


def test_expire_stale_running_then_new_attempt():
    journal = _journal()
    first = journal.claim("inv-1", timeout_seconds=1, now=100.0)
    assert first.lease_until > 100.0
    expired = journal.expire_stale("run-1", now=first.lease_until + 1)
    assert expired == ["inv-1"]
    second = journal.claim("inv-1", timeout_seconds=1, now=first.lease_until + 2)
    assert second.attempt == 2
    assert second.execution_id != first.execution_id


def test_old_execution_cannot_restart_loop():
    journal = _journal()
    first = journal.claim("inv-1", timeout_seconds=10)
    store = MemoryTranscriptStore()
    old = Transcript(
        store,
        TranscriptScope("user-1", "task-1", "component-bbh", first.execution_id),
    )
    run_agent_loop(
        model=_ScriptedModel([AIMessage(content="结论")]),
        tools=[echo],
        system="sys",
        user="user",
        max_steps=2,
        history_max_chars=2000,
        transcript=old,
    )
    with pytest.raises(TranscriptConflict):
        run_agent_loop(
            model=_ScriptedModel([AIMessage(content="再来")]),
            tools=[echo],
            system="sys",
            user="user",
            max_steps=2,
            history_max_chars=2000,
            transcript=old,
        )
    journal.expire_owned("inv-1", first.owner_token)
    second = journal.claim("inv-1", timeout_seconds=10)
    fresh = Transcript(
        store,
        TranscriptScope("user-1", "task-1", "component-bbh", second.execution_id),
    )
    result = run_agent_loop(
        model=_ScriptedModel([AIMessage(content="新 attempt")]),
        tools=[echo],
        system="sys",
        user="user",
        max_steps=2,
        history_max_chars=2000,
        transcript=fresh,
    )
    assert result.messages[-1].content == "新 attempt"
    assert fresh.get("loop/start") is not None


def test_ensure_run_same_intent_is_idempotent():
    journal = _journal()
    again = journal.ensure_run(
        "run-1",
        user_id="user-1",
        pipeline_id="p1",
        max_rounds=3,
        max_tool_calls=24,
    )
    assert again.run_id == "run-1"
    assert again.pipeline_id == "p1"


def test_ensure_run_rejects_different_identity():
    journal = _journal()
    with pytest.raises(InvestigationConflict, match="run_id") as err:
        journal.ensure_run(
            "run-1",
            user_id="other",
            pipeline_id="p1",
            max_rounds=3,
            max_tool_calls=24,
        )
    assert err.value.code == "run_id_conflict"
    with pytest.raises(InvestigationConflict):
        journal.ensure_run(
            "run-1",
            user_id="user-1",
            pipeline_id="p2",
            max_rounds=3,
            max_tool_calls=24,
        )
    with pytest.raises(InvestigationConflict):
        journal.ensure_run(
            "run-1",
            user_id="user-1",
            pipeline_id="p1",
            max_rounds=9,
            max_tool_calls=24,
        )


def test_persist_pending_same_payload_is_idempotent():
    journal = _journal()
    first = journal.get_task("inv-1")
    again = journal.persist_pending(
        investigation_id="inv-1",
        run_id="run-1",
        round_index=1,
        component="bbh",
        question="时钟",
        log_scope={"pipeline_id": "p1", "component": "bbh", "tail_lines": 200},
    )
    assert first is not None
    assert again.investigation_id == first.investigation_id
    assert again.status == first.status


def test_persist_pending_rejects_different_payload():
    journal = _journal()
    with pytest.raises(InvestigationConflict, match="investigation_id") as err:
        journal.persist_pending(
            investigation_id="inv-1",
            run_id="run-1",
            round_index=1,
            component="bbh",
            question="另一问题",
            log_scope={"pipeline_id": "p1", "component": "bbh", "tail_lines": 200},
        )
    assert err.value.code == "investigation_id_conflict"
    with pytest.raises(InvestigationConflict):
        journal.persist_pending(
            investigation_id="inv-1",
            run_id="run-1",
            round_index=2,
            component="bbh",
            question="时钟",
            log_scope={"pipeline_id": "p1", "component": "bbh", "tail_lines": 200},
        )
    with pytest.raises(InvestigationConflict):
        journal.persist_pending(
            investigation_id="inv-1",
            run_id="run-1",
            round_index=1,
            component="bbh",
            question="时钟",
            log_scope={"pipeline_id": "p1", "component": "bbh", "tail_lines": 800},
        )


def test_finish_succeeded_does_not_add_tool_quota():
    journal = _journal()
    journal.add_used_tool_calls("run-1", 3)
    claimed = journal.claim("inv-1", timeout_seconds=10)
    assert journal.finish_succeeded("inv-1", claimed.owner_token, "ref", tool_calls=9)
    assert journal.get_run("run-1").used_tool_calls == 3
