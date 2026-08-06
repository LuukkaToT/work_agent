from work_agent.analysis.artifacts import ArtifactStore
from work_agent.analysis.schemas import (
    AnalysisPlan,
    DomainAnalysisResult,
    DomainTask,
    EvidenceHit,
    RequirementFact,
    ScenarioType,
    TestScenario,
)
from work_agent.analysis.nodes import _normalize_plan
from work_agent.graph.subgraphs.analysis_flow import build_test_analysis_graph


class FakeRetriever:
    def available_channels(self):
        return ["PUCCH", "PUSCH"]

    def expand_allowed_channels(self, channels):
        return list(dict.fromkeys([*channels, "PUSCH"]))


class FakeResearchGraph:
    def invoke(self, value):
        task = DomainTask.model_validate(value["domain_task"])
        evidence = EvidenceHit(
            chunk_id="chunk-1",
            doc_id="doc-1",
            source_kind="channel",
            channel=task.channels[0] if task.channels else None,
            title="Mock",
            section="测试",
            relative_path="PUCCH/mock.md",
            content="mock evidence",
        )
        scenarios = [
            TestScenario(
                scenario_id=f"{task.task_id}_{kind.value}",
                title=f"{kind.value} 场景",
                domain=task.domain,
                scenario_type=kind,
                channels=task.channels,
                preconditions=["配置已生效"],
                steps=["触发场景"],
                expected_results=["状态可观察"],
                observation_points=["基带日志"],
                evidence_refs=["chunk-1"],
            )
            for kind in task.required_scenario_types
        ]
        result = DomainAnalysisResult(
            task_id=task.task_id,
            domain=task.domain,
            channels=task.channels,
            summary="fake domain analysis",
            scenarios=scenarios,
            evidence=[evidence],
        )
        return {"result": result.model_dump(mode="json"), "audit": []}


def test_analysis_subgraph_writes_structured_artifacts(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "run")

    def fake_structured(schema, messages, **kwargs):
        if schema is RequirementFact:
            return RequirementFact(
                title="PUCCH 资源重配置",
                summary="支持业务态更新 PUCCH 资源。",
                directions=["UL"],
                channels=["PUCCH"],
                retrieval_terms=["PUCCH", "资源重配置"],
            )
        if schema is AnalysisPlan:
            return AnalysisPlan(
                tasks=[
                    DomainTask(
                        task_id="task_pucch",
                        name="PUCCH 分析",
                        domain="PUCCH",
                        channels=["PUCCH"],
                        objectives=["覆盖资源重配置风险"],
                        retrieval_queries=["PUCCH reconfiguration"],
                    )
                ]
            )
        raise AssertionError(schema)

    monkeypatch.setattr(
        "work_agent.analysis.nodes.get_knowledge_retriever",
        lambda: FakeRetriever(),
    )
    monkeypatch.setattr(
        "work_agent.analysis.nodes.artifact_store_for_task",
        lambda task_id: store,
    )
    monkeypatch.setattr(
        "work_agent.analysis.nodes.invoke_structured",
        fake_structured,
    )
    monkeypatch.setattr(
        "work_agent.analysis.nodes.build_domain_research_graph",
        lambda: FakeResearchGraph(),
    )

    result = build_test_analysis_graph().invoke(
        {
            "task_id": "task-test",
            "user_input": "支持业务态更新 PUCCH 资源，请生成测试分析。",
        }
    )

    assert result["summary"]["status"] == "completed"
    assert result["summary"]["scenario_count"] == 5
    assert (tmp_path / "run" / "test_analysis.md").exists()
    assert (tmp_path / "run" / "manifest.json").exists()
    assert result["analysis_path"].endswith("test_analysis.md")


def test_plan_merges_scenario_tasks_for_same_channel():
    fact = RequirementFact(
        title="PUCCH",
        summary="PUCCH 变更",
        channels=["PUCCH"],
    )
    plan = AnalysisPlan(
        tasks=[
            DomainTask(
                task_id="normal",
                name="正常",
                domain="UL",
                channels=["PUCCH"],
                objectives=["正常传输"],
                required_scenario_types=[ScenarioType.NORMAL],
            ),
            DomainTask(
                task_id="recovery",
                name="恢复",
                domain="UL",
                channels=["PUCCH"],
                objectives=["异常恢复"],
                required_scenario_types=[ScenarioType.RECOVERY],
            ),
        ]
    )

    normalized = _normalize_plan(plan, fact, ["PUCCH"])

    assert len(normalized.tasks) == 1
    assert normalized.tasks[0].domain == "PUCCH"
    assert normalized.tasks[0].required_scenario_types == [
        ScenarioType.NORMAL,
        ScenarioType.RECOVERY,
    ]
