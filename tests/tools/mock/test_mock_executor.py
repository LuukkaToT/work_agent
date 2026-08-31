"""MockPipelineTool：create / start / query 契约、用例名自校验、四场景。"""

import uuid

import pytest

from work_agent.tools.mock.executor import MockPipelineTool

VALID_CASES = [
    "HF_20B_PUSCH_1Cell_200M_hf_001",
    "TDD_26a_85_5002_4T_1CC_KPI_TST",
]


def create(ex, *, cases=None, version="27B", env="7.223.50.60"):
    return ex.create(cases or VALID_CASES, version, physical_env=env)


def test_create_validates_params():
    ex = MockPipelineTool()
    with pytest.raises(ValueError):
        ex.create([], "27B", physical_env="7.223.50.60")
    with pytest.raises(ValueError):
        ex.create(VALID_CASES, "", physical_env="7.223.50.60")
    with pytest.raises(ValueError):
        ex.create(VALID_CASES, "27B", physical_env="")
    with pytest.raises(ValueError, match="二选一"):
        ex.create(
            VALID_CASES,
            "27B",
            physical_env="7.223.50.60",
            logic_env="BESA_SDV_2BBH_1BBL",
            logic_constraint="1G_2A",
        )


def test_create_rejects_short_case_names():
    """流水线自己校验用例名；agent 不预检，但 mock 要能模拟拒绝。"""
    ex = MockPipelineTool()
    with pytest.raises(ValueError, match="非法用例名"):
        ex.create(["case_a", "ok"], "27B", physical_env="7.223.50.60")


def test_create_returns_server_pipeline_id():
    ex = MockPipelineTool()
    handle = create(ex)
    assert handle.pipeline_id
    uuid.UUID(handle.pipeline_id)  # 合法 uuid


def test_create_records_options_without_changing_behavior():
    """options 只记录进内部记录，不影响 create/start/query 的行为。"""
    ex = MockPipelineTool()
    handle = ex.create(VALID_CASES, "27B", physical_env="7.223.50.60", options={"debug_mode": True})
    assert ex._runs[handle.pipeline_id].options == {"debug_mode": True}


def test_create_defaults_options_to_empty_dict_when_omitted():
    ex = MockPipelineTool()
    handle = create(ex)
    assert ex._runs[handle.pipeline_id].options == {}


def test_create_logical_mode_records_constraint():
    ex = MockPipelineTool()
    handle = ex.create(
        VALID_CASES,
        "27B",
        logic_env="BESA_SDV_2BBH_1BBL",
        logic_constraint="1G_2A",
    )
    assert handle.env == "BESA_SDV_2BBH_1BBL"
    assert handle.env_kind == "logical"
    assert handle.logic_constraint == "1G_2A"


def test_create_logical_mode_requires_constraint():
    ex = MockPipelineTool()
    with pytest.raises(ValueError, match="logic_constraint"):
        ex.create(VALID_CASES, "27B", logic_env="BESA_SDV_2BBH_1BBL")


def test_start_then_query_ticks():
    ex = MockPipelineTool(scenario="all_pass", ticks_to_finish=2)
    handle = create(ex)
    assert ex.start(handle.pipeline_id) is True

    first = ex.query(handle.pipeline_id)
    assert first.phase == "running"
    assert first.results == []

    second = ex.query(handle.pipeline_id)
    assert second.phase == "finished"
    assert [r.verdict for r in second.results] == ["pass", "pass"]


def test_query_before_start_is_created():
    ex = MockPipelineTool()
    handle = create(ex)
    pr = ex.query(handle.pipeline_id)
    assert pr.phase == "created"


def test_version_fail_marks_first_case():
    ex = MockPipelineTool(scenario="version_fail", ticks_to_finish=1)
    handle = create(ex)
    ex.start(handle.pipeline_id)
    pr = ex.query(handle.pipeline_id)
    first, second = pr.results
    assert (first.verdict, first.fail_kind) == ("fail", "version")
    assert second.verdict == "pass"


def test_case_error_marks_first_case():
    ex = MockPipelineTool(scenario="case_error", ticks_to_finish=1)
    handle = create(ex)
    ex.start(handle.pipeline_id)
    first, second = ex.query(handle.pipeline_id).results
    assert (first.verdict, first.fail_kind) == ("error", "case")
    assert second.verdict == "pass"


def test_env_error_fails_on_start():
    ex = MockPipelineTool(scenario="env_error", ticks_to_finish=5)
    handle = create(ex)
    ex.start(handle.pipeline_id)
    pr = ex.query(handle.pipeline_id)
    assert pr.phase == "failed"
    assert all(r.fail_kind == "env" for r in pr.results)


def test_unknown_pipeline_id():
    ex = MockPipelineTool()
    with pytest.raises(KeyError):
        ex.query("no-such-pipeline")


def test_multi_create_distinct_ids():
    """多环境 = 多次 create = 多个 pipeline_id。"""
    ex = MockPipelineTool()
    a = create(ex, env="7.223.50.60")
    b = create(ex, env="7.223.60.11")
    assert a.pipeline_id != b.pipeline_id
