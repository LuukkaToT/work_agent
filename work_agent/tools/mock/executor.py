from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from work_agent.tools.models import CaseResult, RunHandle, RunStatus

MockScenario = Literal["all_pass", "version_fail", "case_error", "env_error"]


@dataclass
class _RunRecord:
    handle: RunHandle
    ticks: int = 0
    finished: bool = False
    results: list[CaseResult] = field(default_factory=list)


class MockExecutor:
    """
    假执行引擎，用来把图的分支跑通。接公司系统后由 real 实现替换。

    四场景：
    - all_pass      全部通过
    - version_fail  首条失败且 fail_kind=version
    - case_error    首条 error 且 fail_kind=case
    - env_error     status 直接 failed（环境不可用）

    ticks_to_finish：
    - status 被调用几次后才变 finished
    - 默认 2：正好演示 exec_poll 自循环（第 1 次 running，第 2 次 finished）
    """

    def __init__(
        self,
        scenario: MockScenario = "all_pass",
        ticks_to_finish: int = 2,
    ) -> None:
        self.scenario = scenario
        self.ticks_to_finish = max(1, ticks_to_finish)
        # run_id → 内存中的执行记录（所以同一进程内必须复用本实例）
        self._runs: dict[str, _RunRecord] = {}

    def run(
        self,
        case_names: list[str],
        version: str,
        topology: str,
    ) -> RunHandle:
        """只「提交」任务并返回 run_id，不在这里等待跑完。"""
        if not case_names:
            raise ValueError("case_names 不能为空")
        if not version:
            raise ValueError("version 不能为空")
        if not topology:
            raise ValueError("topology 不能为空")

        run_id = f"mock-{uuid.uuid4().hex[:8]}"
        handle = RunHandle(
            run_id=run_id,
            case_names=list(case_names),
            version=version,
            topology=topology,
        )
        self._runs[run_id] = _RunRecord(handle=handle)
        return handle

    def status(self, run_id: str) -> RunStatus:
        """每调用一次，内部 ticks+1；到点后 phase 变为 finished。"""
        rec = self._require(run_id)
        if self.scenario == "env_error":
            rec.finished = True
            return RunStatus(
                run_id=run_id,
                phase="failed",
                progress=0.0,
                message="环境不可用：逻辑组网无可用物理资源",
            )

        rec.ticks += 1
        if rec.ticks < self.ticks_to_finish:
            return RunStatus(
                run_id=run_id,
                phase="running",
                progress=rec.ticks / self.ticks_to_finish,
                message=f"执行中 {rec.ticks}/{self.ticks_to_finish}",
            )

        rec.finished = True
        if not rec.results:
            rec.results = self._build_results(rec.handle)
        return RunStatus(
            run_id=run_id,
            phase="finished",
            progress=1.0,
            message="执行完成",
        )

    def results(self, run_id: str) -> list[CaseResult]:
        rec = self._require(run_id)
        if not rec.finished:
            raise RuntimeError(f"run {run_id} 尚未结束，不能取结果")
        if not rec.results:
            rec.results = self._build_results(rec.handle)
        return list(rec.results)

    def logs(self, run_id: str, case_name: str | None = None) -> str:
        rec = self._require(run_id)
        lines = [
            f"[mock] run_id={run_id}",
            f"version={rec.handle.version} topology={rec.handle.topology}",
            f"scenario={self.scenario}",
        ]
        for name in rec.handle.case_names:
            if case_name and name != case_name:
                continue
            lines.append(f"---- {name} ----")
            lines.append(self._log_for_case(name))
        return "\n".join(lines)

    def _require(self, run_id: str) -> _RunRecord:
        if run_id not in self._runs:
            raise KeyError(f"未知 run_id: {run_id}")
        return self._runs[run_id]

    def _build_results(self, handle: RunHandle) -> list[CaseResult]:
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

    def _log_for_case(self, case_name: str) -> str:
        if self.scenario == "version_fail":
            return f"{case_name}: VERSION_MISMATCH protocol handshake failed"
        if self.scenario == "case_error":
            return f"{case_name}: Traceback ... KeyError: 'expected_ie'"
        if self.scenario == "env_error":
            return f"{case_name}: ENV_DOWN no healthy node in pool"
        return f"{case_name}: PASS"