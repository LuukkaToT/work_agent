"""
公司系统的「插座」定义。

图和节点只依赖这里的 Protocol，不依赖 mock 或 real 的类名。
接公司 tool 时：在 tools/real/ 写实现 → registry 切换 → 图不用改。

@runtime_checkable：允许 isinstance(obj, CaseProvider) 做运行时检查（lesson 里用过）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from work_agent.tools.models import CaseInfo, PipelineHandle, PipelineResult


@runtime_checkable
class CaseProvider(Protocol):
    """用例库：列目录 / 按名拉取。执行链路暂不用；分析场景以后再用。"""

    def list_cases(self, query: str | None = None) -> list[CaseInfo]: ...

    def fetch_cases(self, names: list[str]) -> list[CaseInfo]: ...


@runtime_checkable
class PipelineTool(Protocol):
    """
    公司流水线三件套（函数名刻意贴近真实拼写 init_pipline / check_pipline）：

      init_pipline  创建流水线（用例名由流水线自己校验）
      check_pipline 启动执行
      query_result  查询执行数据（用户主动问进度时用）

    env 现阶段只支持物理 IP；逻辑组网后续再加。
    """

    def init_pipline(
        self,
        run_id: str,
        case_names: list[str],
        version: str,
        env: str,
    ) -> PipelineHandle: ...

    def check_pipline(self, run_id: str) -> bool: ...

    def query_result(self, run_id: str) -> PipelineResult: ...
