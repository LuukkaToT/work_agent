"""RunLedger：用临时目录建库，不碰 workspace/index.db。"""

from work_agent.core.identity import DEFAULT_USER_ID
from work_agent.core.ledger import RunLedger


def make_ledger(tmp_path):
    return RunLedger(tmp_path / "index.db")


def insert(
    ledger,
    pipeline_id,
    *,
    case_names=None,
    status="running",
    report_path="",
    env="7.223.50.60",
    task_id=None,
):
    ledger.upsert(
        pipeline_id=pipeline_id,
        task_id=task_id or f"task-{pipeline_id}",
        case_names=case_names or ["HF_20B_PUSCH_001"],
        version="27B",
        env=env,
        status=status,
        report_path=report_path,
    )


def test_upsert_and_get(tmp_path):
    ledger = make_ledger(tmp_path)
    insert(ledger, "r1")
    rec = ledger.get("r1")
    assert rec is not None
    assert rec.case_names == ["HF_20B_PUSCH_001"]
    assert rec.env == "7.223.50.60"
    assert rec.status == "running"
    assert ledger.get("no-such-id") is None


def test_upsert_conflict_keeps_old_report_path(tmp_path):
    ledger = make_ledger(tmp_path)
    insert(ledger, "r1", report_path="D:/x/report.md")
    insert(ledger, "r1", status="finished", report_path="")
    rec = ledger.get("r1")
    assert rec.status == "finished"
    assert rec.report_path == "D:/x/report.md"


def test_update_status_partial(tmp_path):
    ledger = make_ledger(tmp_path)
    insert(ledger, "r1", report_path="D:/x/report.md")

    ledger.update_status("r1", status="finished")
    rec = ledger.get("r1")
    assert rec.status == "finished"
    assert rec.report_path == "D:/x/report.md"

    ledger.update_status("r1", report_path="D:/y/report.md")
    rec = ledger.get("r1")
    assert rec.status == "finished"
    assert rec.report_path == "D:/y/report.md"


def test_latest_orders_by_created_at(tmp_path):
    ledger = make_ledger(tmp_path)
    insert(ledger, "r1")
    insert(ledger, "r2")
    insert(ledger, "r3")
    latest = ledger.latest(limit=2)
    assert [r.pipeline_id for r in latest] == ["r3", "r2"]


def test_find_by_case_exact_match(tmp_path):
    ledger = make_ledger(tmp_path)
    insert(ledger, "r1", case_names=["case_a_long"])
    insert(ledger, "r2", case_names=["case_a_long_extra"])
    insert(ledger, "r3", case_names=["case_b_long", "case_a_long"])

    found = ledger.find_by_case("case_a_long")
    assert {r.pipeline_id for r in found} == {"r1", "r3"}


def test_find_by_task(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.upsert(
        pipeline_id="p1",
        task_id="task-x",
        case_names=["HF_A_001"],
        version="27B",
        env="7.223.50.60",
        status="running",
    )
    ledger.upsert(
        pipeline_id="p2",
        task_id="task-x",
        case_names=["HF_B_001"],
        version="27B",
        env="7.223.60.11",
        status="running",
    )
    ledger.upsert(
        pipeline_id="p3",
        task_id="task-y",
        case_names=["HF_C_001"],
        version="27A",
        env="7.223.50.60",
        status="running",
    )
    found = ledger.find_by_task("task-x")
    assert [r.pipeline_id for r in found] == ["p1", "p2"]


def test_legacy_empty_user_id_backfilled_on_next_startup(tmp_path):
    """历史空 user_id 记录：下次启动（重新构造 RunLedger）时应被回填成默认身份。"""
    db_path = tmp_path / "index.db"
    ledger = RunLedger(db_path)
    insert(ledger, "r1")  # 默认 user_id=""，模拟身份接线前留下的老记录
    assert ledger.get("r1").user_id == ""

    # 重新构造指向同一个库：_init_db 里的回填该跑一次
    ledger2 = RunLedger(db_path)
    rec = ledger2.get("r1")
    assert rec is not None
    assert rec.user_id == DEFAULT_USER_ID


def test_backfill_is_idempotent_and_does_not_touch_real_user_ids(tmp_path):
    db_path = tmp_path / "index.db"
    ledger = RunLedger(db_path)
    insert(ledger, "legacy")
    ledger.upsert(
        pipeline_id="owned",
        task_id="task-owned",
        case_names=["HF_A_001"],
        version="27B",
        env="7.223.50.60",
        status="running",
        user_id="z00888363",
    )

    # 连续两次重新构造：回填只影响空串，不影响已有真实身份，且重复跑不出错
    for _ in range(2):
        fresh = RunLedger(db_path)
        assert fresh.get("legacy").user_id == DEFAULT_USER_ID
        assert fresh.get("owned").user_id == "z00888363"


def test_replace_id_swaps_primary_key(tmp_path):
    ledger = make_ledger(tmp_path)
    insert(
        ledger,
        "local-abc",
        status="creating",
        task_id="task-1",
        env="7.223.50.60",
    )
    ledger.replace_id(
        "local-abc",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        status="created",
    )
    assert ledger.get("local-abc") is None
    rec = ledger.get("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert rec is not None
    assert rec.status == "created"
    assert rec.task_id == "task-1"
    assert rec.env == "7.223.50.60"
