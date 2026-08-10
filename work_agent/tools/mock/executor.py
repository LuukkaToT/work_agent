"""
Mock 流水线 tool：create / start / query。
pipeline_id 由 mock 生成 uuid，模拟服务端返回。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Literal

from work_agent.tools.models import CaseResult, PipelineHandle, PipelineResult

MockScenario = Literal["all_pass", "version_fail", "case_error", "env_error"]

_CASE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{7,}$")


@dataclass
class _PipelineRecord:
    """单条 mock 流水线的内存状态。"""

    handle: PipelineHandle
    started: bool = False
    ticks: int = 0
    finished: bool = False
    results: list[CaseResult] = field(default_factory=list)


class MockPipelineTool:
    """
    假流水线。四场景：all_pass / version_fail / case_error / env_error。
    ticks_to_finish：query 被调用几次后才变 finished。
    """

    def __init__(
        self,
        scenario: MockScenario = "all_pass",
        ticks_to_finish: int = 2,
    ) -> None:
        """
        参数:
            scenario: 预置故障/通过场景。
            ticks_to_finish: 启动后需几次 query 才 finished。
        """
        self.scenario = scenario
        self.ticks_to_finish = max(1, ticks_to_finish)
        self._runs: dict[str, _PipelineRecord] = {}

    def create(
        self,
        case_names: list[str],
        version: str,
        env: str,
    ) -> PipelineHandle:
        """
        创建流水线并生成 uuid 作为 pipeline_id。

        参数:
            case_names: 用例名列表。
            version: 版本。
            env: 组网 IP。

        返回:
            PipelineHandle；参数非法时抛 ValueError。
        """
        if not case_names:
            raise ValueError("case_names 不能为空")
        if not version:
            raise ValueError("version 不能为空")
        if not env:
            raise ValueError("env 不能为空")

        bad = [n for n in case_names if not _CASE_NAME_RE.match(n)]
        if bad:
            raise ValueError(f"流水线拒绝非法用例名: {bad}")

        pipeline_id = str(uuid.uuid4())
        handle = PipelineHandle(
            pipeline_id=pipeline_id,
            case_names=list(case_names),
            version=version,
            env=env,
        )
        self._runs[pipeline_id] = _PipelineRecord(handle=handle)
        return handle

    def start(self, pipeline_id: str) -> bool:
        """
        标记流水线已启动（env_error 场景会立即失败）。

        参数:
            pipeline_id: create 返回的 id。

        返回:
            是否成功受理。
        """
        rec = self._require(pipeline_id)
        if rec.started:
            return True
        if self.scenario == "env_error":
            rec.started = True
            rec.finished = True
            rec.results = self._build_results(rec.handle)
            return True
        rec.started = True
        return True

    def query(self, pipeline_id: str) -> PipelineResult:
        """
        查询流水线阶段与用例结果（按 ticks 推进）。

        参数:
            pipeline_id: 流水线 id。

        返回:
            PipelineResult。
        """
        rec = self._require(pipeline_id)
        if not rec.started:
            return PipelineResult(
                pipeline_id=pipeline_id,
                phase="created",
                message="流水线已创建，尚未启动",
            )

        if self.scenario == "env_error":
            if not rec.results:
                rec.results = self._build_results(rec.handle)
            return PipelineResult(
                pipeline_id=pipeline_id,
                phase="failed",
                results=list(rec.results),
                message="环境不可用：物理节点无响应",
            )

        rec.ticks += 1
        if rec.ticks < self.ticks_to_finish:
            return PipelineResult(
                pipeline_id=pipeline_id,
                phase="running",
                message=f"执行中 {rec.ticks}/{self.ticks_to_finish}",
            )

        rec.finished = True
        if not rec.results:
            rec.results = self._build_results(rec.handle)
        return PipelineResult(
            pipeline_id=pipeline_id,
            phase="finished",
            results=list(rec.results),
            message="执行完成",
        )

    def _require(self, pipeline_id: str) -> _PipelineRecord:
        """按 id 取内存记录；未知 id 抛 KeyError。"""
        if pipeline_id not in self._runs:
            raise KeyError(f"未知 pipeline_id: {pipeline_id}")
        return self._runs[pipeline_id]

    def _build_results(self, handle: PipelineHandle) -> list[CaseResult]:
        """按 scenario 生成各用例 verdict。"""
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
