"""
Tool 层的数据结构（与 Protocol 返回值对应）。

用 dataclass 而不是随便 dict：字段有名字、有类型，接公司接口时也好对照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


RunPhase = Literal["pending", "created", "running", "finished", "failed", "timeout"]
CaseVerdict = Literal["pass", "fail", "error", "skipped"]
FailKind = Literal["none", "version", "case", "env"]


@dataclass(frozen=True)
class CaseInfo:
    """用例目录条目。"""

    name: str  # 用例唯一名
    title: str  # 展示标题
    tags: list[str] = field(default_factory=list)  # 可选标签


@dataclass(frozen=True)
class PipelineHandle:
    """create 成功后的句柄；pipeline_id 由服务端返回。"""

    pipeline_id: str  # 服务端流水线 id
    case_names: list[str]  # 本流水线包含的用例
    version: str  # 软件版本
    env: str  # 展示用环境：物理 IP 或 logic_env 字符串
    env_kind: str = "physical"  # physical | logical
    logic_constraint: str = ""  # 逻辑模式才有；物理模式为空


@dataclass(frozen=True)
class CaseResult:
    """单条用例在流水线中的执行结果。"""

    case_name: str  # 用例名
    verdict: CaseVerdict  # pass / fail / error / skipped
    fail_kind: FailKind = "none"  # 失败归类；通过时为 none
    detail: str = ""  # 附加说明或错误摘要


@dataclass(frozen=True)
class PipelineResult:
    """query 返回：状态 + 各用例结果。"""

    pipeline_id: str  # 流水线 id
    phase: RunPhase  # 整体阶段
    results: list[CaseResult] = field(default_factory=list)  # 用例级结果
    message: str = ""  # 人类可读状态说明


@dataclass(frozen=True)
class SheetTable:
    """用例表原始内容：表头 + 行（全部字符串，不做类型猜测）。"""

    headers: list[str]  # 列名
    rows: list[list[str]]  # 数据行，与 headers 对齐
    path: str = ""  # 来源路径（可选）
