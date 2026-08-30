"""20 组分组件 mock benchmark 的数据完整性与读取契约。"""

from __future__ import annotations

import re

import pytest

from work_agent.eval.runner import load_cases
from work_agent.tools.mock.logs import MockLogTool
from work_agent.tools.mock.executor import MockPipelineTool
from work_agent.tools.mock.scenarios import LOG_COMPONENTS, load_benchmark_scenarios


def test_catalog_has_twenty_unique_scenarios_and_six_logs_each():
    scenarios = load_benchmark_scenarios()
    assert len(scenarios) == 20
    assert len({item.scenario for item in scenarios}) == 20
    for item in scenarios:
        tool = MockLogTool(item.scenario)
        listing = tool.list_logs("pipeline-test")
        assert '"layered": true' in listing
        assert all(f'"{component}.log"' in listing for component in LOG_COMPONENTS)


def test_every_golden_evidence_key_exists_in_the_layered_logs():
    for scenario in load_benchmark_scenarios():
        text = MockLogTool(scenario.scenario).fetch_logs("pipeline-test", tail_lines=None)
        missing = [key for key in scenario.expected_evidence_keys if key.lower() not in text.lower()]
        assert missing == [], f"{scenario.scenario} 缺 golden 证据: {missing}"


def test_eval_default_is_the_same_twenty_case_suite():
    suite, cases = load_cases()
    assert suite == "baseband_layered_benchmark_v1"
    assert len(cases) == 20
    assert {case.scenario for case in cases} == {
        item.scenario for item in load_benchmark_scenarios()
    }


def test_debug_only_subscription_faults_have_no_error_lines():
    for name in (
        "bench08_rx_subscription_debug_stall",
        "bench09_tx_publication_debug_stall",
    ):
        text = MockLogTool(name).fetch_logs("pipeline-test", tail_lines=None)
        assert not re.search(r"\[ERROR\]", text)
        assert "request_subscribe" in text


def test_transient_retry_control_recovers_without_error():
    text = MockLogTool("bench20_transient_subscription_recovered").fetch_logs(
        "pipeline-test", tail_lines=200
    )
    assert "subscription_state=ESTABLISHED" in text
    assert "verdict=pass" in text
    assert "[ERROR]" not in text


def test_component_filter_and_cross_component_grep():
    tool = MockLogTool("bench06_bbh_clock_unlocked")
    only_bbh = tool.fetch_logs("pipeline-test", tail_lines=30, component="bbh")
    assert "E-BBH-2101" in only_bbh
    assert "E-BBL-3112" not in only_bbh

    merged = tool.grep_logs("pipeline-test", r"E-BB[HL]-", context_lines=0)
    assert "E-BBH-2101" in merged
    assert "E-BBL-3112" in merged


def test_unknown_component_is_rejected():
    tool = MockLogTool("bench01_comm_connection_refused")
    with pytest.raises(ValueError, match="未知日志组件"):
        tool.fetch_logs("pipeline-test", component="phy")


def test_pipeline_status_does_not_leak_benchmark_labels():
    tool = MockPipelineTool("bench06_bbh_clock_unlocked")
    handle = tool.create(
        ["NR_BENCH_006_PTP_UNLOCK"],
        "27B",
        physical_env="7.223.50.65",
    )
    tool.start(handle.pipeline_id)
    status = tool.query(handle.pipeline_id)
    rendered = repr(status).lower()
    assert status.phase == "failed"
    assert status.results[0].fail_kind == "unknown"
    assert "bbh" not in rendered
    assert "clock" not in rendered
