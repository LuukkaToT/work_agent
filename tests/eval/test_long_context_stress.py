"""打满 20k ReAct 预算的脚本化 A/B：假模型、固定轨迹、不调真 LLM。"""

from __future__ import annotations

from work_agent.eval.long_context import STRESS_CASES, make_scripted_diagnose
from work_agent.eval.runner import run_case, run_suite
from work_agent.tools.registry import get_log_tool, get_pipeline_tool


def _run_pair(monkeypatch, case):
    rows = []
    for strategy in ("legacy", "managed"):
        rows.append(
            run_case(
                case,
                strategy=strategy,
                suite="long_context_stress_v1",
                run_id="stress-test",
                diagnose=make_scripted_diagnose(monkeypatch, case),
            )
        )
    return {row["strategy"]: row for row in rows}


def test_failure_cases_trim_and_cost_not_above_legacy(monkeypatch):
    get_pipeline_tool.cache_clear()
    get_log_tool.cache_clear()
    failures = [case for case in STRESS_CASES if case.scenario != "all_pass"]
    for case in failures:
        pair = _run_pair(monkeypatch, case)
        for strategy in ("legacy", "managed"):
            assert pair[strategy]["error"] == "", pair[strategy]["error"]
            assert pair[strategy]["evidence_recall"] == 1.0, (
                case.case_id, strategy, pair[strategy]["missing_evidence"]
            )
        managed, legacy = pair["managed"], pair["legacy"]
        assert managed["trimmed_steps"] >= 1, case.case_id
        assert legacy["trimmed_steps"] == 0, case.case_id
        assert managed["react_context_chars"] < legacy["react_context_chars"], case.case_id
        assert managed["token_total"] <= legacy["token_total"], (
            case.case_id, managed["token_total"], legacy["token_total"]
        )
        assert managed["react_prompt_chars_sum"] <= legacy["react_prompt_chars_sum"], case.case_id


def test_all_pass_does_not_require_trim(monkeypatch):
    get_pipeline_tool.cache_clear()
    get_log_tool.cache_clear()
    case = next(item for item in STRESS_CASES if item.scenario == "all_pass")
    pair = _run_pair(monkeypatch, case)
    for row in pair.values():
        assert row["error"] == ""
        assert row["evidence_recall"] == 1.0
        assert row["trimmed_steps"] == 0


def test_run_suite_smoke_writes_stage_tokens(tmp_path, monkeypatch):
    get_pipeline_tool.cache_clear()
    get_log_tool.cache_clear()
    case = STRESS_CASES[0]

    def diagnose(**kwargs):
        return make_scripted_diagnose(monkeypatch, case)(**kwargs)

    rows = run_suite(
        cases=[case],
        suite="long_context_stress_v1",
        store_path=tmp_path / "stress.jsonl",
        diagnose=diagnose,
    )
    assert len(rows) == 2
    for row in rows:
        assert row["react_llm_calls"] >= 8
        assert row["extract_llm_calls"] == 1
        assert row["react_prompt_chars_sum"] > row["react_context_chars"] > 0
        assert row["react_token_input"] > 0
