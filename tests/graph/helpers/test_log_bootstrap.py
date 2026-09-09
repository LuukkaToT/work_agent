"""诊断冷启动粗扫：错误码/级联/去重，以及工具失败不炸整轮。"""

from __future__ import annotations

from work_agent.graph.helpers.diagnose_tools import build_diagnose_tools
from work_agent.graph.helpers.log_bootstrap import (
    FailureCue,
    scan_failure_cues,
    _dedupe_cues,
)
from work_agent.graph.helpers.truncate import CharBudget


def _tools(scenario: str = "bench01_comm_connection_refused"):
    return {
        t.name: t
        for t in build_diagnose_tools(scenario=scenario, budget=CharBudget(limit=80_000))
    }


class _Boom:
    def invoke(self, _args):
        raise NotImplementedError("real stub")


def test_layered_scan_extracts_component_and_errorcode():
    text, calls, trace = scan_failure_cues("pipe-bench01", tools=_tools())
    assert "scan_status=ok" in text
    assert "## failure_cues" in text
    assert "E-COMM-1102" in text
    assert "component=comm" in text
    assert "probable_components=" in text
    assert "路由提示" in text
    assert calls >= 3
    names = [item["name"] for item in trace if item.get("type") == "call"]
    assert names.count("grep_logs") == 2
    assert "lookup_error_code" in names


def test_cascade_hits_are_flagged():
    text, _calls, _trace = scan_failure_cues(
        "pipe-cascade",
        tools=_tools("bench14_cell_load_cascade_from_rat"),
    )
    assert "cascade=true" in text
    assert "E-BBL-3104" in text or "DEPENDENCY_FAILED" in text or "E-BBH-2991" in text


def test_handshake_hint_when_subscribe_has_no_ack():
    text, _calls, _trace = scan_failure_cues(
        "pipe-stall",
        tools=_tools("bench08_rx_subscription_debug_stall"),
    )
    assert "request_subscribe=" in text
    assert "有请求无 ack" in text
    assert "component=bbh" in text


def test_error_noise_is_deduped_and_overview_is_bounded():
    cues = [
        FailureCue("all", "2026-08-09 10:04:01", "", False, f"ERROR cache miss noise_seq={i}")
        for i in range(8)
    ]
    cues.extend(
        FailureCue("all", "2026-08-09 10:04:02", "", False, "ERROR cache miss noise_seq=0")
        for _ in range(5)
    )
    kept = _dedupe_cues(cues)
    assert len(kept) == 8
    text, _calls, _trace = scan_failure_cues(
        "pipe-legacy",
        tools=_tools("case_error"),
        max_matches=24,
    )
    assert "scan_status=ok" in text
    assert len(text) <= 1500
    assert text.count("excerpt=") <= 12


def test_scan_failed_when_grep_missing_or_raises():
    empty, calls, _trace = scan_failure_cues("pipe-x", tools={})
    assert "scan_status=scan_failed" in empty
    assert calls == 0

    boom, charged, _trace = scan_failure_cues(
        "pipe-x",
        tools={"grep_logs": _Boom()},
        on_tool_start=lambda: None,
    )
    assert "scan_status=scan_failed" in boom
    assert "NotImplementedError" in boom or "real stub" in boom
    assert charged == 1


def test_empty_pipeline_id_does_not_call_tools():
    charged = []
    text, calls, _trace = scan_failure_cues(
        "  ",
        tools=_tools(),
        on_tool_start=lambda: charged.append(1),
    )
    assert "scan_failed" in text
    assert calls == 0
    assert charged == []
