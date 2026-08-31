"""pipeline 消解：所有 / 序号 / pick 回复。连测试库读台账。"""

from __future__ import annotations

import uuid

from work_agent.graph.helpers.pipeline_resolve import (
    _match_pick,
    _parse_ordinal,
    resolve_pipeline_records,
)


def _insert(ledger, pid: str, *, task_id: str, env: str, user_id: str) -> None:
    ledger.upsert(
        pipeline_id=pid,
        task_id=task_id,
        case_names=["CaseA_235T_nmimo"],
        version="27B",
        env=env,
        status="running",
        user_id=user_id,
    )


def test_parse_ordinal():
    assert _parse_ordinal("第一条") == 1
    assert _parse_ordinal("第2条") == 2
    assert _parse_ordinal("第一个") == 1
    assert _parse_ordinal("1") == 1
    assert _parse_ordinal("随便说说") is None


def test_resolve_all_skips_pick(pg_ledger, test_user_id):
    p1, p2 = f"p-{uuid.uuid4().hex[:10]}", f"p-{uuid.uuid4().hex[:10]}"
    _insert(pg_ledger, p1, task_id="t1", env="7.223.1.9", user_id=test_user_id)
    _insert(pg_ledger, p2, task_id="t2", env="7.223.10.11", user_id=test_user_id)

    found = resolve_pipeline_records(
        "当前所有流水线的执行状态是什么？", user_id=test_user_id
    )
    assert {r.pipeline_id for r in found} == {p1, p2}


def test_resolve_first_ordinal(pg_ledger, test_user_id):
    older = f"p-{uuid.uuid4().hex[:10]}"
    newer = f"p-{uuid.uuid4().hex[:10]}"
    _insert(pg_ledger, older, task_id="t1", env="7.223.10.11", user_id=test_user_id)
    _insert(pg_ledger, newer, task_id="t2", env="7.223.1.9", user_id=test_user_id)

    found = resolve_pipeline_records(
        "第一条流水线执行的怎么样了？", user_id=test_user_id
    )
    # list_recent 倒序：第一条 = 最新
    assert [r.pipeline_id for r in found] == [newer]


def test_match_pick_ordinal_and_prefix(pg_ledger, test_user_id):
    p1 = str(uuid.uuid4())
    p2 = str(uuid.uuid4())
    _insert(pg_ledger, p1, task_id="t1", env="7.223.1.9", user_id=test_user_id)
    _insert(pg_ledger, p2, task_id="t2", env="7.223.10.11", user_id=test_user_id)
    cands = pg_ledger.list_recent(limit=5, user_id=test_user_id)

    assert _match_pick("第一条", cands).pipeline_id == cands[0].pipeline_id
    assert _match_pick("1", cands).pipeline_id == cands[0].pipeline_id
    assert _match_pick(p2[:8], cands).pipeline_id == p2
    assert _match_pick("7.223.10.11", cands).env == "7.223.10.11"


def test_resolve_pipeline_records_filters_by_user_id(pg_ledger, make_user_id):
    """两个不同 user_id 的记录：传自己的 user_id 只看得到自己的。"""
    alice = make_user_id()
    bob = make_user_id()
    p_alice = f"p-{uuid.uuid4().hex[:10]}"
    p_bob = f"p-{uuid.uuid4().hex[:10]}"
    _insert(pg_ledger, p_alice, task_id="t1", env="7.223.1.9", user_id=alice)
    _insert(pg_ledger, p_bob, task_id="t2", env="7.223.10.11", user_id=bob)

    found = resolve_pipeline_records(
        "当前所有流水线的执行状态是什么？", user_id=alice
    )
    assert [r.pipeline_id for r in found] == [p_alice]

    found_bob = resolve_pipeline_records(
        "当前所有流水线的执行状态是什么？", user_id=bob
    )
    assert [r.pipeline_id for r in found_bob] == [p_bob]
