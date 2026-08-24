"""core/logic_topologies.py：逻辑组网目录查询与种子。"""

from __future__ import annotations

from work_agent.core.logic_topologies import (
    find_logic_topology_candidates,
    is_valid_logic_topology,
    lookup_logic_topology,
)


def test_seeded_catalog_has_placeholder_row(pg_env):
    rec = lookup_logic_topology("BESA_SDV_2BBH_1BBL", "1G_2A")
    assert rec is not None
    assert rec.bbh_count == 2
    assert rec.bbl_count == 1
    assert rec.bbh_board == "G"
    assert rec.bbl_board == "A"
    assert is_valid_logic_topology("BESA_SDV_2BBH_1BBL", "1G_2A") is True


def test_lookup_miss(pg_env):
    assert lookup_logic_topology("NO_SUCH_ENV", "1G_2A") is None
    assert lookup_logic_topology("BESA_SDV_2BBH_1BBL", "") is None
    assert is_valid_logic_topology("BESA_SDV_2BBH_1BBL", "9Z_9Z") is False


def test_find_candidates_by_counts(pg_env):
    recs = find_logic_topology_candidates(bbh_count=2, bbl_count=1)
    pairs = {(r.logic_env, r.logic_constraint) for r in recs}
    assert ("BESA_SDV_2BBH_1BBL", "1G_2A") in pairs
    assert ("BESA_SDV_2BBH_1BBL", "2G_1A") in pairs
    assert all(r.bbh_count == 2 and r.bbl_count == 1 for r in recs)


def test_find_candidates_without_filter_is_empty(pg_env):
    assert find_logic_topology_candidates() == []


def test_find_candidates_by_alias_text(pg_env):
    recs = find_logic_topology_candidates(text="2BBH+1BBL")
    assert recs
    assert all(r.logic_env == "BESA_SDV_2BBH_1BBL" for r in recs)
