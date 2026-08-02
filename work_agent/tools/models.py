"""
Tool 层的数据结构（与 Protocol 返回值对应）。

用 dataclass 而不是随便 dict：字段有名字、有类型，接公司接口时也好对照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


RunPhase = Literal["pending", "running", "finished", "failed", "timeout"]
CaseVerdict = Literal["pass", "fail", "error", "skipped"]
# 失败归因粗分类（MVP 只在 mock 结果里带上；自动归因是 Phase 2）
FailKind = Literal["none", "version", "case", "env"]


@dataclass(frozen=True)
class CaseInfo:
    name: str
    title: str
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PipelineHandle:
    """init_pipline 成功后的句柄。"""

    run_id: str
    case_names: list[str]
    version: str
    env: str  # 物理组网 IP，如 7.223.50.60


@dataclass(frozen=True)
class CaseResult:
    case_name: str
    verdict: CaseVerdict
    fail_kind: FailKind = "none"
    detail: str = ""


@dataclass(frozen=True)
class PipelineResult:
    """query_result 返回：状态 + 各用例结果。"""

    run_id: str
    phase: RunPhase
    results: list[CaseResult] = field(default_factory=list)
    message: str = ""
