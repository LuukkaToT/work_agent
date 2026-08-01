"""RunLedger：用临时目录建库，不碰 workspace/index.db。"""

from work_agent.core.ledger import RunLedger


def make_ledger(tmp_path):
    return RunLedger(tmp_path / "index.db")


def insert(ledger, run_id, *, case_names=None, status="running", report_path=""):
    ledger.upsert(
        run_id=run_id,
        task_id=f"task-{run_id}",
        case_names=case_names or ["case_downlink_001"],
        version="27B",
        topology="topo_a",
        status=status,
        report_path=report_path,
    )


def test_upsert_and_get(tmp_path):
    ledger = make_ledger(tmp_path)
    insert(ledger, "r1")
    rec = ledger.get("r1")
    assert rec is not None
    assert rec.case_names == ["case_downlink_001"]
    assert rec.status == "running"
    assert ledger.get("no-such-id") is None


def test_upsert_conflict_keeps_old_report_path(tmp_path):
    """二次 upsert 传空 report_path 时，不能把已有路径抹掉。"""
    ledger = make_ledger(tmp_path)
    insert(ledger, "r1", report_path="D:/x/report.md")
    insert(ledger, "r1", status="finished", report_path="")
    rec = ledger.get("r1")
    assert rec.status == "finished"
    assert rec.report_path == "D:/x/report.md"


def test_update_status_partial(tmp_path):
    """只更新 status 时 report_path 保持不变，反之亦然。"""
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
    assert [r.run_id for r in latest] == ["r3", "r2"]


def test_find_by_case_exact_match(tmp_path):
    """按用例名找 run，且不能把 case_a 误配到 case_a_extra。"""
    ledger = make_ledger(tmp_path)
    insert(ledger, "r1", case_names=["case_a"])
    insert(ledger, "r2", case_names=["case_a_extra"])
    insert(ledger, "r3", case_names=["case_b", "case_a"])

    found = ledger.find_by_case("case_a")
    assert {r.run_id for r in found} == {"r1", "r3"}
