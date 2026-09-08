"""Postgres investigation journal：claim / fencing；无测试库则 skip。"""

from __future__ import annotations

import uuid

from work_agent.core.investigation_journal import InvestigationConflict, PostgresInvestigationJournal


def _cleanup(pool, run_id: str) -> None:
    with pool.connection() as conn:
        conn.execute("DELETE FROM diagnosis_run WHERE run_id=%s", (run_id,))


def test_postgres_claim_fencing_and_expire(pg_pool):
    run_id = "run-" + uuid.uuid4().hex
    inv_id = "inv-" + uuid.uuid4().hex
    journal = PostgresInvestigationJournal(pool=pg_pool)
    try:
        journal.ensure_run(
            run_id,
            user_id="pg-user",
            pipeline_id="p1",
            max_rounds=3,
            max_tool_calls=24,
        )
        journal.persist_pending(
            investigation_id=inv_id,
            run_id=run_id,
            round_index=1,
            component="bbh",
            question="时钟",
            log_scope={"pipeline_id": "p1", "component": "bbh", "tail_lines": 50},
        )
        first = journal.claim(inv_id, timeout_seconds=5)
        assert first.status == "RUNNING"
        assert first.attempt == 1
        assert journal.expire_owned(inv_id, first.owner_token)
        second = journal.claim(inv_id, timeout_seconds=5)
        assert second.attempt == 2
        assert second.execution_id != first.execution_id
        assert journal.finish_succeeded(inv_id, first.owner_token, "late") is False
        journal.add_used_tool_calls(run_id, 3)
        assert journal.finish_succeeded(inv_id, second.owner_token, "canonical")
        row = journal.get_task(inv_id)
        assert row is not None
        assert row.status == "SUCCEEDED"
        assert row.result_ref == "canonical"
        run = journal.get_run(run_id)
        assert run is not None
        assert run.used_tool_calls == 3
        replay = journal.claim(inv_id, timeout_seconds=5)
        assert replay.status == "SUCCEEDED"
        assert replay.execution_id == second.execution_id
    finally:
        _cleanup(pg_pool, run_id)


def test_postgres_ensure_and_persist_conflict(pg_pool):
    run_id = "run-" + uuid.uuid4().hex
    inv_id = "inv-" + uuid.uuid4().hex
    journal = PostgresInvestigationJournal(pool=pg_pool)
    try:
        journal.ensure_run(
            run_id,
            user_id="pg-user",
            pipeline_id="p1",
            max_rounds=3,
            max_tool_calls=24,
        )
        again = journal.ensure_run(
            run_id,
            user_id="pg-user",
            pipeline_id="p1",
            max_rounds=3,
            max_tool_calls=24,
        )
        assert again.pipeline_id == "p1"
        try:
            journal.ensure_run(
                run_id,
                user_id="pg-user",
                pipeline_id="p-other",
                max_rounds=3,
                max_tool_calls=24,
            )
            raise AssertionError("expected run_id_conflict")
        except InvestigationConflict as exc:
            assert exc.code == "run_id_conflict"
        journal.persist_pending(
            investigation_id=inv_id,
            run_id=run_id,
            round_index=1,
            component="bbh",
            question="时钟",
            log_scope={"pipeline_id": "p1", "component": "bbh", "tail_lines": 50},
        )
        journal.persist_pending(
            investigation_id=inv_id,
            run_id=run_id,
            round_index=1,
            component="bbh",
            question="时钟",
            log_scope={"pipeline_id": "p1", "component": "bbh", "tail_lines": 50},
        )
        try:
            journal.persist_pending(
                investigation_id=inv_id,
                run_id=run_id,
                round_index=1,
                component="bbh",
                question="另一问",
                log_scope={"pipeline_id": "p1", "component": "bbh", "tail_lines": 50},
            )
            raise AssertionError("expected investigation_id_conflict")
        except InvestigationConflict as exc:
            assert exc.code == "investigation_id_conflict"
    finally:
        _cleanup(pg_pool, run_id)
