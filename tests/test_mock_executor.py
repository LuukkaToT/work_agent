"""MockPipelineTool：三函数契约、用例名自校验、四场景。"""

import pytest

from work_agent.tools.mock.executor import MockPipelineTool

VALID_CASES = [
    "HF_20B_PUSCH_1Cell_200M_hf_001",
    "TDD_26a_85_5002_4T_1CC_KPI_TST",
]


def create(ex, *, run_id="pipe-001", cases=None, version="27B", env="7.223.50.60"):
    return ex.init_pipline(
        run_id,
        cases or VALID_CASES,
        version,
        env,
    )


def test_init_validates_params():
    ex = MockPipelineTool()
    with pytest.raises(ValueError):
        ex.init_pipline("", VALID_CASES, "27B", "7.223.50.60")
    with pytest.raises(ValueError):
        ex.init_pipline("pipe-1", [], "27B", "7.223.50.60")
    with pytest.raises(ValueError):
        ex.init_pipline("pipe-1", VALID_CASES, "", "7.223.50.60")
    with pytest.raises(ValueError):
        ex.init_pipline("pipe-1", VALID_CASES, "27B", "")


def test_init_rejects_short_case_names():
    """流水线自己校验用例名；agent 不预检，但 mock 要能模拟拒绝。"""
    ex = MockPipelineTool()
    with pytest.raises(ValueError, match="非法用例名"):
        ex.init_pipline("pipe-1", ["case_a", "ok"], "27B", "7.223.50.60")


def test_check_then_query_ticks():
    ex = MockPipelineTool(scenario="all_pass", ticks_to_finish=2)
    create(ex)
    assert ex.check_pipline("pipe-001") is True

    first = ex.query_result("pipe-001")
    assert first.phase == "running"
    assert first.results == []

    second = ex.query_result("pipe-001")
    assert second.phase == "finished"
    assert [r.verdict for r in second.results] == ["pass", "pass"]


def test_query_before_check_is_pending():
    ex = MockPipelineTool()
    create(ex)
    pr = ex.query_result("pipe-001")
    assert pr.phase == "pending"


def test_version_fail_marks_first_case():
    ex = MockPipelineTool(scenario="version_fail", ticks_to_finish=1)
    create(ex)
    ex.check_pipline("pipe-001")
    pr = ex.query_result("pipe-001")
    first, second = pr.results
    assert (first.verdict, first.fail_kind) == ("fail", "version")
    assert second.verdict == "pass"


def test_case_error_marks_first_case():
    ex = MockPipelineTool(scenario="case_error", ticks_to_finish=1)
    create(ex)
    ex.check_pipline("pipe-001")
    first, second = ex.query_result("pipe-001").results
    assert (first.verdict, first.fail_kind) == ("error", "case")
    assert second.verdict == "pass"


def test_env_error_fails_on_check():
    ex = MockPipelineTool(scenario="env_error", ticks_to_finish=5)
    create(ex)
    ex.check_pipline("pipe-001")
    pr = ex.query_result("pipe-001")
    assert pr.phase == "failed"
    assert all(r.fail_kind == "env" for r in pr.results)


def test_unknown_run_id():
    ex = MockPipelineTool()
    with pytest.raises(KeyError):
        ex.query_result("no-such-run")


def test_duplicate_run_id_rejected():
    ex = MockPipelineTool()
    create(ex)
    with pytest.raises(ValueError, match="已存在"):
        create(ex)
