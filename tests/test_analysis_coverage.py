from work_agent.analysis.coverage import review_coverage
from work_agent.analysis.schemas import (
    DomainAnalysisResult,
    DomainTask,
    ScenarioType,
    TestScenario,
)


def _scenario(kind: ScenarioType, *, complete: bool = True) -> TestScenario:
    return TestScenario(
        scenario_id=f"case_{kind.value}",
        title=f"{kind.value} 场景",
        domain="PUCCH",
        scenario_type=kind,
        channels=["PUCCH"],
        preconditions=["配置已生效"] if complete else [],
        steps=["触发业务"] if complete else [],
        expected_results=["行为符合资料定义"] if complete else [],
        observation_points=["基带日志"] if complete else [],
        evidence_refs=["chunk-1"] if complete else [],
    )


def test_review_coverage_finds_missing_types_and_fields():
    task = DomainTask(
        task_id="t1",
        name="PUCCH",
        domain="PUCCH",
        channels=["PUCCH"],
        objectives=["分析"],
    )
    result = DomainAnalysisResult(
        task_id="t1",
        domain="PUCCH",
        scenarios=[_scenario(ScenarioType.NORMAL, complete=False)],
    )

    gaps = review_coverage([task], [result])

    missing_types = {
        gap.missing_item
        for gap in gaps
        if gap.dimension == "scenario_type"
    }
    assert {
        "boundary",
        "abnormal",
        "reconfiguration",
        "recovery",
    }.issubset(missing_types)
    assert {gap.dimension for gap in gaps}.issuperset(
        {"precondition", "steps", "expected_result", "observation", "evidence"}
    )


def test_review_coverage_passes_complete_minimum_set():
    task = DomainTask(
        task_id="t1",
        name="PUCCH",
        domain="PUCCH",
        channels=["PUCCH"],
        objectives=["分析"],
    )
    result = DomainAnalysisResult(
        task_id="t1",
        domain="PUCCH",
        scenarios=[_scenario(kind) for kind in task.required_scenario_types],
    )

    assert review_coverage([task], [result]) == []
