"""诊断契约：无命中不得写成组件正常；范围扩大可补查。"""

from __future__ import annotations

import pytest

from work_agent.graph.helpers.diagnosis_models import (
    ComponentReport,
    Evidence,
    InvestigationTask,
    LogScope,
    MainDecision,
)


def _task(**kwargs):
    payload = {
        "investigation_id": "inv-1",
        "diagnosis_task_id": "task-1",
        "round_index": 1,
        "component": "bbh",
        "question": "时钟是否失锁",
        "pipeline_id": "p1",
        "log_scope": LogScope(pipeline_id="p1", component="bbh", tail_lines=200),
    }
    payload.update(kwargs)
    return InvestigationTask.model_validate(payload)


def test_no_hit_cannot_claim_component_healthy():
    report = ComponentReport(
        investigation_id="inv-1",
        component="comm",
        status="no_hit",
        findings=["COMM 组件正常，无需处理"],
        coverage="",
        missing=[],
    )
    assert report.status == "no_hit"
    assert "COMM 组件正常，无需处理" not in report.findings
    assert "不能据此判定组件正常" in report.findings[0]
    assert report.coverage
    assert report.missing


def test_log_scope_expansion_and_fingerprint():
    narrow = LogScope(pipeline_id="p1", component="bbh", tail_lines=100)
    wide = LogScope(pipeline_id="p1", component="bbh", tail_lines=400)
    same = LogScope(pipeline_id="p1", component="bbh", tail_lines=100)
    assert wide.covers_more_than(narrow)
    assert not narrow.covers_more_than(wide)
    assert same.fingerprint() == narrow.fingerprint()
    assert wide.fingerprint() != narrow.fingerprint()


def test_investigation_fingerprint_includes_question_and_scope():
    a = _task()
    b = _task(question="时钟是否失锁")
    c = _task(question="订阅是否建立")
    d = _task(
        investigation_id="inv-2",
        log_scope=LogScope(pipeline_id="p1", component="bbh", tail_lines=800),
    )
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()
    assert a.fingerprint() != d.fingerprint()


def test_investigate_action_requires_tasks_and_caps_at_three():
    with pytest.raises(ValueError, match="至少包含一条"):
        MainDecision(action="investigate")
    specs = [
        {"component": name, "question": f"查 {name}"}
        for name in ("comm", "rat", "bbh", "bbl")
    ]
    decision = MainDecision(action="investigate", investigations=specs)
    assert len(decision.investigations) == 3


def test_evidence_requires_artifact_and_explicit_missing_locator():
    item = Evidence(
        evidence_id="ev-1",
        source="log",
        component="bbh",
        artifact_id="sha256_" + "a" * 64,
        excerpt="ptp_state=UNLOCKED",
    )
    assert item.file == "missing"
    assert item.line == "missing"
    assert item.timestamp == "missing"
    assert item.content_hash()
