"""
并行组件诊断的核心契约。

字段缺省必须显式：没有时间/行号就标 missing，不能靠调用方脑补。
无命中不能写成「组件正常」。知识检索只能当旁证。结论引用必须能解析到已存证据。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from work_agent.tools.mock.scenarios import LOG_COMPONENTS

ComponentId = Literal["comm", "rat", "bbh", "bbl", "marp", "compare"]
ReportStatus = Literal["ok", "timeout", "tool_error", "no_hit"]
HypothesisStatus = Literal["open", "supported", "rejected", "uncertain"]
EvidenceSource = Literal["log", "knowledge"]
MainAction = Literal["investigate", "conclude", "insufficient_evidence"]
FailKind = Literal["version", "case", "env", "unknown", "none"]

_MISSING = "missing"
_BANNED_HEALTHY = ("组件正常", "一切正常", "无异常", "工作正常")


def _norm_component(value: str) -> str:
    text = (value or "").strip().casefold()
    if text not in LOG_COMPONENTS:
        raise ValueError(f"组件必须是 {LOG_COMPONENTS} 之一，收到 {value!r}")
    return text


class LogScope(BaseModel):
    """一次调查覆盖的日志范围。缺时间或行号时填 missing，不能假装已扫完全量。"""

    pipeline_id: str = Field(description="锁定的流水线 ID")
    component: str = Field(description="锁定的日志组件名")
    tail_lines: int | None = Field(default=None, description="尾部行数；None 表示未限定")
    start_ts: str = Field(default=_MISSING, description="起始时间；未知必须填 missing")
    end_ts: str = Field(default=_MISSING, description="结束时间；未知必须填 missing")
    version_tag: str = Field(default="", description="版本标记，来自流水线 brief")

    @field_validator("component")
    @classmethod
    def _component_id(cls, value: str) -> str:
        return _norm_component(value)

    @field_validator("pipeline_id")
    @classmethod
    def _pipeline_id(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("pipeline_id 不能为空")
        return text

    @field_validator("start_ts", "end_ts")
    @classmethod
    def _explicit_missing(cls, value: str) -> str:
        text = (value or "").strip() or _MISSING
        return text

    def fingerprint(self) -> str:
        """同范围判定用的稳定键。"""
        return "|".join(
            [
                self.pipeline_id,
                self.component,
                str(self.tail_lines if self.tail_lines is not None else ""),
                self.start_ts,
                self.end_ts,
            ]
        )

    def covers_more_than(self, other: "LogScope") -> bool:
        """
        是否在同一流水线/组件上扩大了范围。

        允许补查的条件：尾部行数变大，或时间窗变宽。缩范围不算扩大。
        """
        if self.pipeline_id != other.pipeline_id or self.component != other.component:
            return False
        wider_tail = (self.tail_lines or 0) > (other.tail_lines or 0)
        wider_start = (
            self.start_ts != _MISSING
            and other.start_ts != _MISSING
            and self.start_ts < other.start_ts
        )
        wider_end = (
            self.end_ts != _MISSING
            and other.end_ts != _MISSING
            and self.end_ts > other.end_ts
        )
        newly_bounded = (
            (other.start_ts == _MISSING and self.start_ts != _MISSING)
            or (other.end_ts == _MISSING and self.end_ts != _MISSING)
        )
        return wider_tail or wider_start or wider_end or newly_bounded


class InvestigationBudget(BaseModel):
    """单次组件调查的步数、超时与上下文预算。派发前由运行时切分。"""

    max_steps: int = Field(default=4, ge=1, description="最多模型决策次数")
    timeout_seconds: float = Field(default=90.0, gt=0, description="单组件超时秒数")
    history_max_chars: int = Field(default=12000, ge=1, description="transcript 投影字符预算")
    tool_result_max_chars: int = Field(default=8000, ge=1, description="单次工具结果记账上限")


class InvestigationTask(BaseModel):
    """
    一条待派发的组件调查。

    investigation_id 必须在派发前生成；同一 ID 已完成则幂等返回旧报告，不重跑。
    """

    investigation_id: str = Field(description="派发前生成的调查 ID")
    diagnosis_task_id: str = Field(description="诊断任务 ID，与主 ReAct 共享")
    round_index: int = Field(ge=1, description="主循环轮次，从 1 起")
    component: str = Field(description="目标组件")
    question: str = Field(description="待验证问题，不能为空")
    related_hypothesis_ids: list[str] = Field(default_factory=list, description="关联假设 ID")
    pipeline_id: str = Field(description="锁定的流水线")
    log_scope: LogScope
    budget: InvestigationBudget = Field(default_factory=InvestigationBudget)

    @field_validator("investigation_id", "diagnosis_task_id", "pipeline_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("标识字段不能为空")
        return text

    @field_validator("component")
    @classmethod
    def _component_id(cls, value: str) -> str:
        return _norm_component(value)

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        text = " ".join((value or "").split())
        if not text:
            raise ValueError("待验证问题不能为空")
        return text

    @model_validator(mode="after")
    def _scope_matches(self) -> "InvestigationTask":
        if self.log_scope.pipeline_id != self.pipeline_id:
            raise ValueError("log_scope.pipeline_id 必须与任务 pipeline_id 一致")
        if self.log_scope.component != self.component:
            raise ValueError("log_scope.component 必须与任务 component 一致")
        return self

    def fingerprint(self) -> str:
        """同组件 + 同问题 + 同日志范围。"""
        return "|".join([self.component, self.question.casefold(), self.log_scope.fingerprint()])


class Evidence(BaseModel):
    """
    一条已落盘证据。

    原文在 artifact_id 指向的 transcript 归档里；报告只保留引用。
    file / line / timestamp 未知时必须填 missing。
    """

    evidence_id: str = Field(description="证据 ID，结论引用必须能解析到此")
    source: EvidenceSource = Field(description="log 为主证，knowledge 只能当旁证")
    component: str
    file: str = Field(default=_MISSING, description="日志文件；未知填 missing")
    line: str = Field(default=_MISSING, description="行号或行标识；未知填 missing")
    timestamp: str = Field(default=_MISSING, description="日志时间；未知填 missing")
    artifact_id: str = Field(description="原文 artifact 引用")
    excerpt: str = Field(default="", description="短摘，供去重与主上下文；不是全文")
    collection_condition: str = Field(default="", description="采集时的 pipeline/组件/过滤条件")

    @field_validator("evidence_id", "artifact_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("evidence_id / artifact_id 不能为空")
        return text

    @field_validator("component")
    @classmethod
    def _component_id(cls, value: str) -> str:
        return _norm_component(value)

    @field_validator("file", "line", "timestamp")
    @classmethod
    def _explicit_missing(cls, value: str) -> str:
        return (value or "").strip() or _MISSING

    def content_hash(self) -> str:
        """按来源、组件、摘录与原文引用做内容去重键。"""
        import hashlib

        payload = "|".join([self.source, self.component, self.excerpt, self.artifact_id])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ComponentReport(BaseModel):
    """
    单次组件调查的结构化报告。

    status=no_hit 表示范围内没有命中，必须写清覆盖范围与缺失，禁止「组件正常」。
    """

    investigation_id: str
    component: str
    status: ReportStatus
    findings: list[str] = Field(default_factory=list, description="发现摘要，无命中不得写组件正常")
    evidence_ids: list[str] = Field(default_factory=list)
    supports_hypotheses: list[str] = Field(default_factory=list)
    contradicts_hypotheses: list[str] = Field(default_factory=list)
    coverage: str = Field(default="", description="实际覆盖的文件/时间/过滤条件")
    missing: list[str] = Field(default_factory=list, description="本轮仍缺的信息")
    suggested_followups: list[str] = Field(
        default_factory=list, description="跨组件补查建议，仅供主 ReAct 下轮派发"
    )
    tool_calls: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)

    @field_validator("investigation_id")
    @classmethod
    def _inv_id(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("investigation_id 不能为空")
        return text

    @field_validator("component")
    @classmethod
    def _component_id(cls, value: str) -> str:
        return _norm_component(value)

    @model_validator(mode="after")
    def _no_hit_not_healthy(self) -> "ComponentReport":
        if self.status != "no_hit":
            return self
        cleaned = [
            item
            for item in self.findings
            if not any(token in item for token in _BANNED_HEALTHY)
        ]
        if not cleaned:
            cleaned = ["指定范围内未命中相关日志，不能据此判定组件正常"]
        self.findings = cleaned
        if not self.coverage.strip():
            self.coverage = "未记录覆盖范围"
        if not self.missing:
            self.missing = ["无命中条件下的充分覆盖证明"]
        return self


class Hypothesis(BaseModel):
    """候选根因假设。支持/反证引用的是证据 ID，不是自由文本。"""

    hypothesis_id: str
    description: str
    candidate_components: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    status: HypothesisStatus = "open"

    @field_validator("hypothesis_id", "description")
    @classmethod
    def _required(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("hypothesis_id / description 不能为空")
        return text

    @field_validator("candidate_components")
    @classmethod
    def _components(cls, value: list[str]) -> list[str]:
        return [_norm_component(item) for item in value]


class InvestigateSpec(BaseModel):
    """主 ReAct 本轮要派发的一条调查（尚未生成 investigation_id）。"""

    component: str
    question: str
    related_hypothesis_ids: list[str] = Field(default_factory=list)
    tail_lines: int | None = Field(default=200, description="本轮日志尾部行数")
    start_ts: str = Field(default=_MISSING)
    end_ts: str = Field(default=_MISSING)

    @field_validator("component")
    @classmethod
    def _component_id(cls, value: str) -> str:
        return _norm_component(value)

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        text = " ".join((value or "").split())
        if not text:
            raise ValueError("待验证问题不能为空")
        return text


class MainDecision(BaseModel):
    """主 ReAct 每一轮的结构化动作。"""

    action: MainAction
    investigations: list[InvestigateSpec] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    fail_kind: str = Field(default="unknown")
    root_component: str = Field(default="unknown")
    root_cause: str = Field(default="unknown")
    evidence: str = Field(default="")
    evidence_ids: list[str] = Field(default_factory=list)
    conclusion: str = Field(default="")
    suggestion: str = Field(default="")
    ruled_out: list[dict] = Field(default_factory=list)
    stop_reason: str = Field(default="")
    missing_info: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _investigate_has_tasks(self) -> "MainDecision":
        if self.action == "investigate" and not self.investigations:
            raise ValueError("investigate 动作必须至少包含一条调查任务")
        if self.action != "investigate":
            self.investigations = []
        elif len(self.investigations) > 3:
            self.investigations = self.investigations[:3]
        return self
