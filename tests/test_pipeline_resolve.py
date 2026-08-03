"""pipeline 消解：所有 / 序号 / pick 回复。"""

from __future__ import annotations

from work_agent.core.ledger import RunLedger
from work_agent.graph.nodes import pipeline_resolve as mod
from work_agent.graph.nodes.pipeline_resolve import (
    _match_pick,
    _parse_ordinal,
    resolve_pipeline_records,
)


def _insert(ledger: RunLedger, pid: str, *, task_id: str, env: str) -> None:
    ledger.upsert(
        pipeline_id=pid,
        task_id=task_id,
        case_names=["CaseA_235T_nmimo"],
        version="27B",
        env=env,
        status="running",
    )


def test_parse_ordinal():
    assert _parse_ordinal("第一条") == 1
    assert _parse_ordinal("第2条") == 2
    assert _parse_ordinal("第一个") == 1
    assert _parse_ordinal("1") == 1
    assert _parse_ordinal("随便说说") is None


def test_resolve_all_skips_pick(tmp_path, monkeypatch):
    ledger = RunLedger(tmp_path / "index.db")
    _insert(ledger, "p1", task_id="t1", env="7.223.1.9")
    _insert(ledger, "p2", task_id="t2", env="7.223.10.11")
    monkeypatch.setattr(mod, "get_ledger", lambda: ledger)

    found = resolve_pipeline_records("当前所有流水线的执行状态是什么？")
    assert {r.pipeline_id for r in found} == {"p1", "p2"}


def test_resolve_first_ordinal(tmp_path, monkeypatch):
    ledger = RunLedger(tmp_path / "index.db")
    _insert(ledger, "older", task_id="t1", env="7.223.10.11")
    _insert(ledger, "newer", task_id="t2", env="7.223.1.9")
    monkeypatch.setattr(mod, "get_ledger", lambda: ledger)

    found = resolve_pipeline_records("第一条流水线执行的怎么样了？")
    # list_recent 倒序：第一条 = 最新
    assert [r.pipeline_id for r in found] == ["newer"]


def test_match_pick_ordinal_and_prefix(tmp_path, monkeypatch):
    ledger = RunLedger(tmp_path / "index.db")
    _insert(
        ledger,
        "932f222b-d6a1-4150-a818-e6ca048cff65",
        task_id="t1",
        env="7.223.1.9",
    )
    _insert(
        ledger,
        "77942b18-5041-42a2-9ac6-e9fb84c7326e",
        task_id="t2",
        env="7.223.10.11",
    )
    monkeypatch.setattr(mod, "get_ledger", lambda: ledger)
    cands = ledger.list_recent(limit=5)

    assert _match_pick("第一条", cands).pipeline_id == cands[0].pipeline_id
    assert _match_pick("1", cands).pipeline_id == cands[0].pipeline_id
    assert _match_pick("77942b18", cands).pipeline_id.startswith("77942b18")
    assert _match_pick("7.223.10.11", cands).env == "7.223.10.11"
