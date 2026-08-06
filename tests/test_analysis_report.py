from work_agent.analysis.report import render_markdown
from work_agent.analysis.schemas import (
    DomainAnalysisResult,
    EvidenceHit,
    RequirementFact,
    ScenarioType,
    TestScenario,
)


def test_report_contains_scenarios_coverage_and_evidence():
    fact = RequirementFact(
        title="PUCCH 资源重配置",
        summary="支持业务态更新 PUCCH 资源。",
        channels=["PUCCH"],
        directions=["UL"],
    )
    evidence = EvidenceHit(
        chunk_id="chunk-1",
        doc_id="doc-1",
        source_kind="channel",
        channel="PUCCH",
        title="PUCCH 资料",
        section="重配置",
        relative_path="PUCCH/a.md",
        content="检查资源切换。",
    )
    scenario = TestScenario(
        scenario_id="case_pucch_reconfig",
        title="业务态资源重配置",
        domain="PUCCH",
        scenario_type=ScenarioType.RECONFIGURATION,
        channels=["PUCCH"],
        preconditions=["业务已建立"],
        steps=["下发新配置"],
        expected_results=["新资源生效"],
        observation_points=["RRC/PHY 日志"],
        evidence_refs=["chunk-1"],
    )
    result = DomainAnalysisResult(
        task_id="t1",
        domain="PUCCH",
        channels=["PUCCH"],
        summary="覆盖资源重配置。",
        scenarios=[scenario],
        evidence=[evidence],
    )

    markdown = render_markdown(fact, [result], [])

    assert "# 测试分析：PUCCH 资源重配置" in markdown
    assert "原始需求" in markdown
    assert "case_pucch_reconfig" in markdown
    assert "`chunk-1`" in markdown
    assert "已通过当前代码规则" in markdown
