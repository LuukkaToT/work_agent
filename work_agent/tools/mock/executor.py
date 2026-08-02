"""
Mock 流水线 tool：对齐公司 init_pipline / check_pipline / query_result。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from work_agent.tools.models import CaseResult, PipelineHandle, PipelineResult

MockScenario = Literal["all_pass", "version_fail", "case_error", "env_error"]

# 真实用例名很长；mock 用宽松规则模拟「流水线自己校验」：
# 至少 8 个字符，含下划线或连字符，不以纯数字开头。
_CASE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{7,}$")


@dataclass
class _PipelineRecord:
    handle: PipelineHandle
    started: bool = False
    ticks: int = 0
    finished: bool = False
    results: list[CaseResult] = field(default_factory=list)


class MockPipelineTool:
    """
    假流水线，用来把图的分支跑通。接公司系统后由 real 实现替换。

    四场景：
    - all_pass      全部通过
    - version_fail  首条失败且 fail_kind=version
    - case_error    首条 error 且 fail_kind=case
    - env_error     query 直接 failed（环境不可用）

    ticks_to_finish：
    - query_result 被调用几次后才变 finished（模拟分钟级执行）
    """

    def __init__(
        self,
        scenario: MockScenario = "all_pass",
        ticks_to_finish: int = 2,
    ) -> None:
        self.scenario = scenario
        self.ticks_to_finish = max(1, ticks_to_finish)
        self._runs: dict[str, _PipelineRecord] = {}

    def init_pipline(
        self,
        run_id: str,
        case_names: list[str],
        version: str,
        env: str,
    ) -> PipelineHandle:
        if not run_id:
            raise ValueError("run_id 不能为空")
        if not case_names:
            raise ValueError("case_names 不能为空")
        if not version:
            raise ValueError("version 不能为空")
        if not env:
            raise ValueError("env 不能为空")

        bad = [n for n in case_names if not _CASE_NAME_RE.match(n)]
        if bad:
            raise ValueError(f"流水线拒绝非法用例名: {bad}")

        if run_id in self._runs:
            raise ValueError(f"run_id 已存在: {run_id}")

        handle = PipelineHandle(
            run_id=run_id,
            case_names=list(case_names),
            version=version,
            env=env,
        )
        self._runs[run_id] = _PipelineRecord(handle=handle)
        return handle

    def check_pipline(self, run_id: str) -> bool:
        rec = self._require(run_id)
        if rec.started:
            return True
        if self.scenario == "env_error":
            rec.started = True
            rec.finished = True
            rec.results = self._build_results(rec.handle)
            return True
        rec.started = True
        return True

    def query_result(self, run_id: str) -> PipelineResult:
        rec = self._require(run_id)
        if not rec.started:
            return PipelineResult(
                run_id=run_id,
                phase="pending",
                message="流水线已创建，尚未启动",
            )

        if self.scenario == "env_error":
            if not rec.results:
                rec.results = self._build_results(rec.handle)
            return PipelineResult(
                run_id=run_id,
                phase="failed",
                results=list(rec.results),
                message="环境不可用：物理节点无响应",
            )

        rec.ticks += 1
        if rec.ticks < self.ticks_to_finish:
            return PipelineResult(
                run_id=run_id,
                phase="running",
                message=f"执行中 {rec.ticks}/{self.ticks_to_finish}",
            )

        rec.finished = True
        if not rec.results:
            rec.results = self._build_results(rec.handle)
        return PipelineResult(
            run_id=run_id,
            phase="finished",
            results=list(rec.results),
            message="执行完成",
        )

    def _require(self, run_id: str) -> _PipelineRecord:
        if run_id not in self._runs:
            raise KeyError(f"未知 run_id: {run_id}")
        return self._runs[run_id]

    def _build_results(self, handle: PipelineHandle) -> list[CaseResult]:
        names = handle.case_names
        if self.scenario == "all_pass":
            return [CaseResult(n, "pass", "none", "断言全部通过") for n in names]
        if self.scenario == "version_fail":
            head, *tail = names
            out = [
                CaseResult(
                    head,
                    "fail",
                    "version",
                    f"版本 {handle.version} 出现协议不匹配",
                )
            ]
            out.extend(CaseResult(n, "pass", "none", "ok") for n in tail)
            return out
        if self.scenario == "case_error":
            head, *tail = names
            out = [CaseResult(head, "error", "case", "用例脚本抛异常: KeyError")]
            out.extend(CaseResult(n, "pass", "none", "ok") for n in tail)
            return out
        return [
            CaseResult(n, "error", "env", "环境不可用，未真正执行") for n in names
        ]
