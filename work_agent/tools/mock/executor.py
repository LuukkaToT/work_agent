"""
Mock 流水线 tool：create / start / query。
pipeline_id 由 mock 生成 uuid，模拟服务端返回。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from work_agent.tools.create_mode import resolve_create_env
from work_agent.tools.mock.scenarios import (
    MockScenario,
    get_benchmark_scenario,
    scenario_fail_kind,
)
from work_agent.tools.models import CaseResult, PipelineHandle, PipelineResult

_CASE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{7,}$")


@dataclass
class _PipelineRecord:
    """单条 mock 流水线的内存状态。"""

    handle: PipelineHandle
    started: bool = False
    ticks: int = 0
    finished: bool = False
    results: list[CaseResult] = field(default_factory=list)
    # 创建时传入的 options（如 debug_mode）；mock 不模拟公司 API 对它的实际
    # 行为差异，只保证透传路径可测。
    options: dict[str, Any] = field(default_factory=dict)


class MockPipelineTool:
    """
    假流水线。兼容四个旧场景，并支持受版本控制的 20 组 benchmark。
    ticks_to_finish：query 被调用几次后才变 finished。
    """

    def __init__(
        self,
        scenario: MockScenario = "all_pass",
        ticks_to_finish: int = 2,
    ) -> None:
        """
        参数:
            scenario: 预置故障/通过场景或 benchmark scenario 名。
            ticks_to_finish: 启动后需几次 query 才 finished。
        """
        # 构造期校验，避免直到日志工具调用时才发现 scenario 拼错。
        scenario_fail_kind(scenario)
        self.scenario = scenario
        self.ticks_to_finish = max(1, ticks_to_finish)
        self._runs: dict[str, _PipelineRecord] = {}

    def create(
        self,
        case_names: list[str],
        version: str,
        *,
        physical_env: str | None = None,
        logic_env: str | None = None,
        logic_constraint: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> PipelineHandle:
        """
        创建流水线并生成 uuid 作为 pipeline_id。

        参数:
            case_names: 用例名列表。
            version: 版本。
            physical_env: 物理组网 IP；与逻辑模式互斥。
            logic_env: 规范逻辑组网名；须与 ``logic_constraint`` 成对。
            logic_constraint: 逻辑约束。
            options: 可选开关（目前只有 ``debug_mode``）；只记录进内部记录，
                不改变 mock 的执行行为（mock 不模拟公司 API 差异）。

        返回:
            PipelineHandle；参数非法时抛 ValueError。
        """
        if not case_names:
            raise ValueError("case_names 不能为空")
        if not version:
            raise ValueError("version 不能为空")
        env_kind, display_env, constraint = resolve_create_env(
            physical_env=physical_env,
            logic_env=logic_env,
            logic_constraint=logic_constraint,
        )

        bad = [n for n in case_names if not _CASE_NAME_RE.match(n)]
        if bad:
            raise ValueError(f"流水线拒绝非法用例名: {bad}")

        pipeline_id = str(uuid.uuid4())
        handle = PipelineHandle(
            pipeline_id=pipeline_id,
            case_names=list(case_names),
            version=version,
            env=display_env,
            env_kind=env_kind,
            logic_constraint=constraint,
        )
        self._runs[pipeline_id] = _PipelineRecord(
            handle=handle, options=dict(options or {})
        )
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
        benchmark = get_benchmark_scenario(self.scenario)
        if benchmark is not None:
            # benchmark 表示一份已经采集完成的离线诊断样本；状态只暴露通用
            # 成败，不泄露 golden fail_kind/root_component。
            rec.started = True
            rec.finished = True
            rec.results = self._build_results(rec.handle)
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

        benchmark = get_benchmark_scenario(self.scenario)
        if benchmark is not None:
            if not rec.results:
                rec.results = self._build_results(rec.handle)
            passed = benchmark.fail_kind == "none"
            return PipelineResult(
                pipeline_id=pipeline_id,
                phase="finished" if passed else "failed",
                results=list(rec.results),
                message=(
                    "执行完成，断言通过"
                    if passed
                    else "流水线执行失败，请结合分组件日志定位根因"
                ),
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
        if self.scenario == "env_error":
            return [
                CaseResult(n, "error", "env", "环境不可用，未真正执行") for n in names
            ]

        benchmark = get_benchmark_scenario(self.scenario)
        if benchmark is None:  # __init__ 已校验；保留防御分支。
            raise ValueError(f"未知 mock scenario: {self.scenario!r}")
        if benchmark.fail_kind == "none":
            return [
                CaseResult(n, "pass", "none", "分层日志无持续故障，瞬态重试已恢复")
                for n in names
            ]
        head, *tail = names
        out = [
            CaseResult(
                head,
                "error",
                "unknown",
                "用例执行失败，状态接口未提供根因，请分析分组件日志",
            )
        ]
        out.extend(
            CaseResult(n, "error", "unknown", "前序用例失败，未提供根因")
            for n in tail
        )
        return out
