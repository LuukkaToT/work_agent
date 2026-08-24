"""PostgresLedger：连 POSTGRES_TEST_DSN 测台账行为。"""

from __future__ import annotations

import uuid

from work_agent.core.ledger import PostgresLedger


def _pid() -> str:
    return f"p-{uuid.uuid4().hex[:12]}"


def insert(
    ledger,
    pipeline_id,
    *,
    case_names=None,
    status="running",
    report_path="",
    env="7.223.50.60",
    task_id=None,
    user_id="",
):
    ledger.upsert(
        pipeline_id=pipeline_id,
        task_id=task_id or f"task-{pipeline_id}",
        case_names=case_names or ["HF_20B_PUSCH_001"],
        version="27B",
        env=env,
        status=status,
        report_path=report_path,
        user_id=user_id,
    )


def test_get_ledger_returns_postgres_ledger(pg_ledger):
    assert isinstance(pg_ledger, PostgresLedger)


def test_upsert_and_get(pg_ledger, test_user_id):
    pid = _pid()
    insert(pg_ledger, pid, user_id=test_user_id)
    rec = pg_ledger.get(pid)
    assert rec is not None
    assert rec.case_names == ["HF_20B_PUSCH_001"]
    assert rec.env == "7.223.50.60"
    assert rec.status == "running"
    assert rec.user_id == test_user_id
    assert pg_ledger.get("no-such-id") is None


def test_upsert_conflict_keeps_old_report_path(pg_ledger, test_user_id):
    pid = _pid()
    insert(pg_ledger, pid, report_path="D:/x/report.md", user_id=test_user_id)
    insert(pg_ledger, pid, status="finished", report_path="", user_id=test_user_id)
    rec = pg_ledger.get(pid)
    assert rec.status == "finished"
    assert rec.report_path == "D:/x/report.md"


def test_update_status_partial(pg_ledger, test_user_id):
    pid = _pid()
    insert(pg_ledger, pid, report_path="D:/x/report.md", user_id=test_user_id)

    pg_ledger.update_status(pid, status="finished")
    rec = pg_ledger.get(pid)
    assert rec.status == "finished"
    assert rec.report_path == "D:/x/report.md"

    pg_ledger.update_status(pid, report_path="D:/y/report.md")
    rec = pg_ledger.get(pid)
    assert rec.status == "finished"
    assert rec.report_path == "D:/y/report.md"


def test_latest_orders_by_created_at(pg_ledger, test_user_id):
    p1, p2, p3 = _pid(), _pid(), _pid()
    insert(pg_ledger, p1, user_id=test_user_id)
    insert(pg_ledger, p2, user_id=test_user_id)
    insert(pg_ledger, p3, user_id=test_user_id)
    latest = pg_ledger.latest(limit=2, user_id=test_user_id)
    assert [r.pipeline_id for r in latest] == [p3, p2]


def test_find_by_case_exact_match(pg_ledger, test_user_id):
    p1, p2, p3 = _pid(), _pid(), _pid()
    insert(pg_ledger, p1, case_names=["case_a_long"], user_id=test_user_id)
    insert(pg_ledger, p2, case_names=["case_a_long_extra"], user_id=test_user_id)
    insert(
        pg_ledger,
        p3,
        case_names=["case_b_long", "case_a_long"],
        user_id=test_user_id,
    )

    found = pg_ledger.find_by_case("case_a_long", user_id=test_user_id)
    assert {r.pipeline_id for r in found} == {p1, p3}


def test_find_by_task(pg_ledger, test_user_id):
    task_x = f"task-x-{uuid.uuid4().hex[:8]}"
    task_y = f"task-y-{uuid.uuid4().hex[:8]}"
    p1, p2, p3 = _pid(), _pid(), _pid()
    pg_ledger.upsert(
        pipeline_id=p1,
        task_id=task_x,
        case_names=["HF_A_001"],
        version="27B",
        env="7.223.50.60",
        status="running",
        user_id=test_user_id,
    )
    pg_ledger.upsert(
        pipeline_id=p2,
        task_id=task_x,
        case_names=["HF_B_001"],
        version="27B",
        env="7.223.60.11",
        status="running",
        user_id=test_user_id,
    )
    pg_ledger.upsert(
        pipeline_id=p3,
        task_id=task_y,
        case_names=["HF_C_001"],
        version="27A",
        env="7.223.50.60",
        status="running",
        user_id=test_user_id,
    )
    found = pg_ledger.find_by_task(task_x, user_id=test_user_id)
    assert [r.pipeline_id for r in found] == [p1, p2]


def test_replace_id_swaps_primary_key(pg_ledger, test_user_id):
    old_id = f"local-{uuid.uuid4().hex[:12]}"
    new_id = str(uuid.uuid4())
    insert(
        pg_ledger,
        old_id,
        status="creating",
        task_id="task-1",
        env="7.223.50.60",
        user_id=test_user_id,
    )
    pg_ledger.replace_id(old_id, new_id, status="created")
    assert pg_ledger.get(old_id) is None
    rec = pg_ledger.get(new_id)
    assert rec is not None
    assert rec.status == "created"
    assert rec.task_id == "task-1"
    assert rec.env == "7.223.50.60"
    assert rec.user_id == test_user_id


def test_get_filters_by_user_id(pg_ledger, make_user_id):
    alice = make_user_id()
    bob = make_user_id()
    pid = _pid()
    insert(pg_ledger, pid, user_id=alice)
    assert pg_ledger.get(pid, user_id=alice) is not None
    assert pg_ledger.get(pid, user_id=bob) is None


def test_find_by_task_isolates_users(pg_ledger, make_user_id):
    alice = make_user_id()
    bob = make_user_id()
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    pid_a, pid_b = _pid(), _pid()
    insert(pg_ledger, pid_a, task_id=task_id, user_id=alice)
    insert(pg_ledger, pid_b, task_id=task_id, env="7.223.60.11", user_id=bob)

    alice_records = pg_ledger.find_by_task(task_id, user_id=alice)
    assert [r.pipeline_id for r in alice_records] == [pid_a]

    all_records = pg_ledger.find_by_task(task_id)
    assert {r.pipeline_id for r in all_records} == {pid_a, pid_b}
