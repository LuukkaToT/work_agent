"""容量 ID 规范化、精确映射与相似容量组推荐。"""

import pytest

from work_agent.core.capacity_mappings import (
    extract_capacity_ids,
    find_similar_capacity_groups,
    lookup_capacity_topologies,
    normalize_capacity_ids,
)


def test_normalize_capacity_group_is_lower_sorted_and_unique():
    assert normalize_capacity_ids(["0x15109C", "0x10108d", "0x15109c"]) == (
        "0x10108d",
        "0x15109c",
    )
    assert extract_capacity_ids("容量 0x15109C、0x10108d") == (
        "0x10108d",
        "0x15109c",
    )


def test_invalid_capacity_id_is_rejected():
    with pytest.raises(ValueError, match="容量 ID"):
        normalize_capacity_ids(["1500c"])


def test_single_capacity_id_exact_mapping(pg_env):
    records = lookup_capacity_topologies(["0x1500C"])
    assert [record.display_name for record in records] == [
        "BESA_NR_SDV_BBH_3BBL,H+SLG"
    ]


def test_capacity_pair_matches_as_an_order_independent_group(pg_env):
    records = lookup_capacity_topologies(["0x15109c", "0x10108d"])
    assert {record.display_name for record in records} == {
        "BESA_NR_SDV_BBH_3BBL,H+SLG",
        "BESA_NR_SDV_BBH_BBL_AI,H+G",
    }


def test_similar_capacity_groups_are_ranked(pg_env):
    candidates = find_similar_capacity_groups(["0x1500e"])
    assert candidates
    assert candidates[0].capacity_ids in {("0x1500c",), ("0x1500d",)}
    assert candidates[0].score >= 0.6
