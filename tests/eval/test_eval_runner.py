"""eval.runner：golden set 加载、evidence_recall、长表落盘、策略汇总。注入 fake 内核，不调 LLM。"""

from __future__ import annotations

import json

import pytest

from work_agent.eval.runner import (
    EvalCase,
    append_records,
    evidence_recall,
    format_report,
    load_cases,
    run_suite,
    summarize,
)
from work_agent.graph.nodes.error_analysis import DiagnosisResult


def _case(
    case_id: str = "c1",
    *,
    scenario: str = "case_error",
    expected: str = "case",
    root: str = "",
    keys: list[str] | None = None,
) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        scenario=scenario,
        user_input="为什么失败",
        pipeline={
            "case_names": ["CaseA_235T_nmimo"],
            "version": "27B",
            "physical_env": "7.223.50.60",
        },
        expected_fail_kind=expected,
        expected_root_component=root,
        expected_evidence_keys=keys if keys is not None else ["KeyError"],
    )


def _fake_diagnose(**kwargs):
    """按策略返回不同代价的假结果：managed 更省字符，但多花摘要 token。"""
    strategy = kwargs.get("context_strategy")
    if strategy == "managed":
        return DiagnosisResult(
            structured={"fail_kind": "case", "root_component": "rat"},
            analysis_text="结论",
            message="msg",
            strategy="managed",
            context_text="【tool_result:fetch_logs】KeyError: 'antenna_map'",
            selected_context_ids=["goal", "obs-001-fetch_logs"],
            context_chars=400,
            latency_ms=120,
            token_usage={"input": 800, "output": 120, "total": 920, "calls": 3},
            tool_calls=3,
            compressed_ids=["obs-002-grep_logs"],
            trimmed_steps=1,
        )
    return DiagnosisResult(
        structured={"fail_kind": "case", "root_component": "rat"},
        analysis_text="结论",
        message="msg",
        strategy="legacy",
        context_text="【evidence_from_tools】KeyError: 'antenna_map'" + "x" * 1000,
        selected_context_ids=[],
        context_chars=1600,
        latency_ms=150,
        token_usage={"input": 1500, "output": 120, "total": 1620, "calls": 2},
        tool_calls=3,
    )


def test_evidence_recall_full_and_partial():
    recall, missing = evidence_recall("日志里出现 KeyError 和 antenna_map", ["KeyError", "antenna_map"])
    assert recall == 1.0
    assert missing == []

    recall, missing = evidence_recall("只剩 KeyError", ["KeyError", "antenna_map"])
    assert recall == 0.5
    assert missing == ["antenna_map"]


def test_evidence_recall_is_case_insensitive():
    recall, missing = evidence_recall("keyerror: 'antenna_map'", ["KeyError"])
    assert recall == 1.0
    assert missing == []


def test_evidence_recall_without_keys_is_one():
    assert evidence_recall("任意内容", []) == (1.0, [])


def test_evidence_recall_zero_when_all_trimmed_away():
    recall, missing = evidence_recall("（原始日志已被裁掉）", ["KeyError", "Traceback"])
    assert recall == 0.0
    assert missing == ["KeyError", "Traceback"]


def test_load_shipped_golden_set_is_valid():
    suite, cases = load_cases()
    assert suite
    assert len(cases) >= 4
    kinds = {c.expected_fail_kind for c in cases}
    # 覆盖 case / version / env / none 四类归因
    assert {"case", "version", "env", "none"} <= kinds
    for c in cases:
        assert c.pipeline.get("case_names"), f"{c.case_id} 缺 case_names"
        assert c.expected_root_component, f"{c.case_id} 缺 expected_root_component"
        assert c.expected_evidence_keys, f"{c.case_id} 缺 expected_evidence_keys"


def test_load_cases_rejects_missing_fields(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"suite": "x", "cases": [{"case_id": "a", "scenario": "case_error"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="expected_fail_kind"):
        load_cases(bad)


def test_load_cases_rejects_empty_suite(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"suite": "x", "cases": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="没有 cases"):
        load_cases(empty)


def test_run_suite_writes_one_row_per_case_and_strategy(tmp_path):
    out = tmp_path / "eval.jsonl"
    rows = run_suite(
        cases=[_case("c1"), _case("c2")],
        suite="t",
        store_path=out,
        diagnose=_fake_diagnose,
    )

    assert len(rows) == 4
    for case_id in ("c1", "c2"):
        got = {r["strategy"] for r in rows if r["case_id"] == case_id}
        assert got == {"legacy", "managed"}

    # 同一批共享 run_id，便于按批次 diff
    assert len({r["run_id"] for r in rows}) == 1

    written = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(written) == 4
    assert all(r["error"] == "" for r in written)


def test_run_suite_records_expected_metrics(tmp_path):
    rows = run_suite(
        cases=[_case("c1", keys=["KeyError", "缺失关键词"])],
        suite="t",
        store_path=tmp_path / "e.jsonl",
        diagnose=_fake_diagnose,
    )
    managed = next(r for r in rows if r["strategy"] == "managed")
    legacy = next(r for r in rows if r["strategy"] == "legacy")

    assert managed["correct"] is True
    assert managed["evidence_recall"] == 0.5
    assert managed["missing_evidence"] == ["缺失关键词"]
    assert managed["latency_ms"] > 0
    assert managed["selected_context_ids"]
    assert managed["pipeline_id"]
    # managed 省了字符，但摘要的 token 必须体现在总量里（calls 更多）
    assert managed["context_chars"] < legacy["context_chars"]
    assert managed["llm_calls"] > legacy["llm_calls"]
    # 期望值只留在 golden 文件里，不复制进结果行
    assert "expected_fail_kind" not in managed
    assert "expected_root_component" not in managed
    assert "expected_evidence_keys" not in managed


def test_run_suite_scores_root_component_without_copying_golden(tmp_path):
    rows = run_suite(
        cases=[_case("c1", root="rat")],
        suite="t",
        store_path=tmp_path / "root.jsonl",
        diagnose=_fake_diagnose,
    )
    assert all(row["root_component"] == "rat" for row in rows)
    assert all(row["root_component_correct"] is True for row in rows)
    assert all(row["diagnosis_correct"] is True for row in rows)
    summary = summarize(rows)
    assert summary["managed"]["root_component_accuracy"] == 1.0


def test_run_suite_marks_wrong_fail_kind_as_incorrect(tmp_path):
    rows = run_suite(
        cases=[_case("c1", expected="env")],
        suite="t",
        store_path=tmp_path / "e.jsonl",
        diagnose=_fake_diagnose,
    )
    assert all(r["correct"] is False for r in rows)


def test_run_suite_survives_single_case_failure(tmp_path):
    def boom(**kwargs):
        raise RuntimeError("模型挂了")

    rows = run_suite(
        cases=[_case("c1")],
        suite="t",
        store_path=tmp_path / "e.jsonl",
        diagnose=boom,
    )
    assert len(rows) == 2
    assert all("模型挂了" in r["error"] for r in rows)
    assert all(r["correct"] is False for r in rows)
    assert all(r["evidence_recall"] == 0.0 for r in rows)


def test_run_suite_can_skip_storing(tmp_path):
    out = tmp_path / "none.jsonl"
    run_suite(
        cases=[_case("c1")],
        suite="t",
        store=False,
        store_path=out,
        diagnose=_fake_diagnose,
    )
    assert not out.exists()


def test_run_suite_single_strategy(tmp_path):
    rows = run_suite(
        cases=[_case("c1")],
        suite="t",
        strategies=("managed",),
        store_path=tmp_path / "e.jsonl",
        diagnose=_fake_diagnose,
    )
    assert [r["strategy"] for r in rows] == ["managed"]


def test_summarize_groups_by_strategy(tmp_path):
    rows = run_suite(
        cases=[_case("c1"), _case("c2")],
        suite="t",
        store_path=tmp_path / "e.jsonl",
        diagnose=_fake_diagnose,
    )
    summary = summarize(rows)
    assert set(summary) == {"legacy", "managed"}
    assert summary["managed"]["n"] == 2
    assert summary["managed"]["accuracy"] == 1.0
    assert summary["managed"]["context_chars"] < summary["legacy"]["context_chars"]
    assert summary["managed"]["errors"] == 0


def test_format_report_shows_context_delta(tmp_path):
    rows = run_suite(
        cases=[_case("c1")],
        suite="t",
        store_path=tmp_path / "e.jsonl",
        diagnose=_fake_diagnose,
    )
    text = format_report(summarize(rows))
    assert "managed" in text and "legacy" in text
    assert "context_chars 变化" in text
    # 小样本准确率必须标注为趋势参考，避免被当成统计结论
    assert "趋势参考" in text


def test_format_report_handles_empty():
    assert "没有可汇总" in format_report({})


def test_append_records_is_append_only(tmp_path):
    out = tmp_path / "e.jsonl"
    append_records([{"a": 1}], path=out)
    append_records([{"a": 2}], path=out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["a"] for x in lines] == [1, 2]
