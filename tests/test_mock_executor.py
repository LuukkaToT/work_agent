"""MockExecutor 四场景与轮询行为：这是执行分支的契约，real 实现也要满足。"""

import pytest

from work_agent.tools.mock.executor import MockExecutor


def submit(ex, cases=None):
    return ex.run(cases or ["case_a", "case_b"], version="27B", topology="topo_a")


def test_run_validates_params():
    ex = MockExecutor()
    with pytest.raises(ValueError):
        ex.run([], "27B", "topo_a")
    with pytest.raises(ValueError):
        ex.run(["case_a"], "", "topo_a")
    with pytest.raises(ValueError):
        ex.run(["case_a"], "27B", "")


def test_polling_ticks_to_finish():
    """ticks_to_finish=2：第 1 次 running，第 2 次 finished，与 exec_poll 自循环对齐。"""
    ex = MockExecutor(scenario="all_pass", ticks_to_finish=2)
    handle = submit(ex)

    first = ex.status(handle.run_id)
    assert first.phase == "running"

    second = ex.status(handle.run_id)
    assert second.phase == "finished"


def test_results_before_finish_raises():
    ex = MockExecutor(scenario="all_pass", ticks_to_finish=2)
    handle = submit(ex)
    with pytest.raises(RuntimeError):
        ex.results(handle.run_id)


def test_all_pass_results():
    ex = MockExecutor(scenario="all_pass", ticks_to_finish=1)
    handle = submit(ex)
    ex.status(handle.run_id)
    results = ex.results(handle.run_id)
    assert [r.verdict for r in results] == ["pass", "pass"]


def test_version_fail_marks_first_case():
    ex = MockExecutor(scenario="version_fail", ticks_to_finish=1)
    handle = submit(ex)
    ex.status(handle.run_id)
    first, second = ex.results(handle.run_id)
    assert (first.verdict, first.fail_kind) == ("fail", "version")
    assert second.verdict == "pass"


def test_case_error_marks_first_case():
    ex = MockExecutor(scenario="case_error", ticks_to_finish=1)
    handle = submit(ex)
    ex.status(handle.run_id)
    first, second = ex.results(handle.run_id)
    assert (first.verdict, first.fail_kind) == ("error", "case")
    assert second.verdict == "pass"


def test_env_error_fails_immediately():
    """环境错误不进入 running，第一次查状态就是 failed。"""
    ex = MockExecutor(scenario="env_error", ticks_to_finish=5)
    handle = submit(ex)
    st = ex.status(handle.run_id)
    assert st.phase == "failed"
    results = ex.results(handle.run_id)
    assert all(r.fail_kind == "env" for r in results)


def test_unknown_run_id():
    ex = MockExecutor()
    with pytest.raises(KeyError):
        ex.status("no-such-run")
