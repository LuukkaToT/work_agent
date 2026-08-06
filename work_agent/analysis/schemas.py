"""测试分析的稳定数据契约。

LLM 只产生这些结构化对象；检索、覆盖检查和 Markdown 渲染均由代码完成。
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar, Literal

from pydantic import BaseModel, Field


class ScenarioType(str, Enum):
    NORMAL = "normal"
    BOUNDARY = "boundary"
    ABNORMAL = "abnormal"
    RECONFIGURATION = "reconfiguration"
    RECOVERY = "recovery"
    CONCURRENCY = "concurrency"
    PERFORMANCE = "performance"
    COMPATIBILITY = "compatibility"


DEFAULT_SCENARIO_TYPES = [
    ScenarioType.NORMAL,
    ScenarioType.BOUNDARY,
    ScenarioType.ABNORMAL,
    ScenarioType.RECONFIGURATION,
    ScenarioType.RECOVERY,
]


class RequirementFact(BaseModel):
    title: str = Field(description="简短、准确的需求标题")
    summary: str = Field(description="不添加外部事实的需求摘要")
    raw_requirement: str = Field(
        default="",
        description="用户原始需求，供报告追溯；不得改写",
    )
    sufficient: bool = Field(
        default=True,
        description="输入是否足以启动初步测试分析",
    )
    product_version: str | None = None
    release: str | None = None
    directions: list[Literal["UL", "DL", "BOTH", "NA"]] = Field(
        default_factory=list
    )
    channels: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    procedures: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    retrieval_terms: list[str] = Field(default_factory=list)


class DomainTask(BaseModel):
    task_id: str
    name: str
    domain: str
    channels: list[str] = Field(default_factory=list)
    objectives: list[str] = Field(default_factory=list)
    required_scenario_types: list[ScenarioType] = Field(
        default_factory=lambda: list(DEFAULT_SCENARIO_TYPES)
    )
    retrieval_queries: list[str] = Field(default_factory=list)


class AnalysisPlan(BaseModel):
    tasks: list[DomainTask] = Field(default_factory=list)
    cross_domain_concerns: list[str] = Field(default_factory=list)


class EvidenceHit(BaseModel):
    chunk_id: str
    doc_id: str
    source_kind: Literal["basic", "channel"]
    channel: str | None = None
    title: str
    section: str
    relative_path: str
    content: str
    score: float = 0.0


class EvidenceAssessment(BaseModel):
    status: Literal[
        "sufficient",
        "retry_search",
        "expand_channels",
        "corpus_gap",
    ]
    covered_topics: list[str] = Field(default_factory=list)
    missing_topics: list[str] = Field(default_factory=list)
    followup_queries: list[str] = Field(default_factory=list)
    additional_channels: list[str] = Field(default_factory=list)
    reason: str = ""


class TestScenario(BaseModel):
    __test__: ClassVar[bool] = False

    scenario_id: str
    title: str
    domain: str
    scenario_type: ScenarioType
    priority: Literal["P0", "P1", "P2"] = "P1"
    channels: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    expected_results: list[str] = Field(default_factory=list)
    observation_points: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class DomainAnalysisResult(BaseModel):
    task_id: str
    domain: str
    channels: list[str] = Field(default_factory=list)
    status: Literal["completed", "partial", "failed"] = "completed"
    summary: str = ""
    scenarios: list[TestScenario] = Field(default_factory=list)
    evidence: list[EvidenceHit] = Field(default_factory=list)
    missing_topics: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CoverageGap(BaseModel):
    task_id: str
    domain: str
    dimension: Literal[
        "scenario_type",
        "precondition",
        "steps",
        "expected_result",
        "observation",
        "evidence",
    ]
    missing_item: str
    reason: str


class AnalysisOutput(BaseModel):
    status: Literal["completed", "partial", "need_input", "failed"]
    summary: str
    report_ref: str | None = None
    warnings: list[str] = Field(default_factory=list)
    failed_domains: list[str] = Field(default_factory=list)
