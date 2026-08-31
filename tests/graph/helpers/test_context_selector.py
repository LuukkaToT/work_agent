"""context_selector：normalize / dedup / 打分 / 预算选择。全纯函数，不需要 mock。"""

from __future__ import annotations

import pytest

from work_agent.graph.helpers.context_budget import (
    PRIORITY_CONCLUSION,
    PRIORITY_EVIDENCE,
    PRIORITY_RAW_TOOL,
)
from work_agent.graph.helpers.context_selector import (
    PIN_IMMUTABLE,
    PIN_NORMAL,
    PIN_PROTECTED,
    ContextItem,
    ImmutableBudgetExceeded,
    dedup_items,
    expand_tokens,
    goal_relevance,
    normalize_items,
    rank_items,
    render_items,
    select,
    select_within_budget,
)


def _item(
    item_id: str,
    text: str,
    *,
    priority: int = PRIORITY_RAW_TOOL,
    pin: str = PIN_NORMAL,
    source: str = "fetch_logs",
    kind: str = "tool_result",
) -> ContextItem:
    return ContextItem(
        item_id=item_id,
        kind=kind,
        source=source,
        text=text,
        priority=priority,
        pin=pin,  # type: ignore[arg-type]
    )


def test_normalize_drops_empty_and_assigns_seq():
    items = normalize_items(
        [_item("a", "  hello  "), _item("blank", "   "), _item("b", "world")]
    )
    assert [i.item_id for i in items] == ["a", "b"]
    assert [i.seq for i in items] == [0, 1]
    assert items[0].text == "hello"


def test_normalize_collapses_blank_runs():
    items = normalize_items([_item("a", "line1\n\n\n\n\nline2")])
    assert items[0].text == "line1\n\nline2"


def test_dedup_removes_exact_duplicates():
    items = normalize_items(
        [_item("a", "ERROR timeout"), _item("b", "error   TIMEOUT"), _item("c", "other")]
    )
    kept, removed = dedup_items(items)
    assert [i.item_id for i in kept] == ["a", "c"]
    assert [i.item_id for i in removed] == ["b"]


def test_dedup_removes_contained_shorter_text_from_same_source():
    # fetch_logs(tail=200) 之后又 fetch_logs(tail=500)：前者是后者的子串
    short = _item("short", "line8\nline9")
    long = _item("long", "line1\nline2\nline8\nline9")
    kept, removed = dedup_items(normalize_items([short, long]))
    assert [i.item_id for i in kept] == ["long"]
    assert [i.item_id for i in removed] == ["short"]


def test_dedup_keeps_same_text_from_different_sources():
    a = _item("a", "same text", source="fetch_logs")
    b = _item("b", "same text", source="grep_logs")
    kept, _ = dedup_items(normalize_items([a, b]))
    assert len(kept) == 2


def test_expand_tokens_covers_chinese_bigrams():
    tokens = expand_tokens("流水线失败")
    assert "流水" in tokens
    assert "水线" in tokens


def test_goal_relevance_prefers_matching_text():
    goal = expand_tokens("case_downlink_001 为什么失败")
    hit = goal_relevance("case_downlink_001 抛出 KeyError", goal)
    miss = goal_relevance("完全无关的内容", goal)
    assert hit > miss


def test_rank_priority_outweighs_recency():
    conclusion = _item("c", "结论草稿", priority=PRIORITY_CONCLUSION)
    raw_new = _item("r", "很新的原始日志", priority=PRIORITY_RAW_TOOL)
    ranked = rank_items(normalize_items([conclusion, raw_new]), goal="结论")
    # 即便 raw 更新（seq 更大），priority 权重也不该被 recency 翻盘
    assert ranked[0].item_id == "c"


def test_rank_gives_evidence_bonus():
    plain = _item("plain", "一切正常的普通输出内容")
    err = _item("err", "发生 KeyError 导致用例中断")
    ranked = rank_items(normalize_items([plain, err]), goal="")
    assert ranked[0].item_id == "err"


def test_immutable_over_budget_raises_instead_of_silent_degrade():
    huge = _item("goal", "g" * 500, pin=PIN_IMMUTABLE, priority=PRIORITY_CONCLUSION)
    with pytest.raises(ImmutableBudgetExceeded, match="immutable"):
        select_within_budget(normalize_items([huge]), goal="x", limit=100)


def test_protected_is_never_dropped_but_flagged_for_compression():
    protected = _item(
        "draft", "d" * 900, pin=PIN_PROTECTED, priority=PRIORITY_CONCLUSION, source="react"
    )
    normal = _item("obs", "o" * 900)
    result = select_within_budget(
        normalize_items([protected, normal]), goal="x", limit=500
    )
    ids = [i.item_id for i in result.selected]
    assert "draft" in ids  # 不允许删除
    assert "obs" not in ids  # normal 被挤掉
    assert [i.item_id for i in result.needs_compression] == ["draft"]
    assert [i.item_id for i in result.dropped] == ["obs"]


def test_normal_items_fill_budget_by_score():
    keep = _item("keep", "KeyError 出现在 case_downlink_001", priority=PRIORITY_EVIDENCE)
    drop = _item("drop", "x" * 400)
    result = select_within_budget(
        normalize_items([keep, drop]), goal="case_downlink_001 KeyError", limit=120
    )
    assert [i.item_id for i in result.selected] == ["keep"]
    assert [i.item_id for i in result.dropped] == ["drop"]
    assert result.total_chars <= 120


def test_select_under_budget_is_not_over_budget():
    result = select([_item("a", "短内容")], goal="短", limit=1000)
    assert result.over_budget is False
    assert [i.item_id for i in result.selected] == ["a"]


def test_render_items_uses_block_headers():
    text = render_items([_item("a", "正文", source="grep_logs")])
    assert text == "【tool_result:grep_logs】\n正文"
