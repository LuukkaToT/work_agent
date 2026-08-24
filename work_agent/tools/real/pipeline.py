"""真实流水线适配层：映射 external SDK → PipelineTool Protocol。"""

from __future__ import annotations

from typing import Any

from work_agent.tools.create_mode import resolve_create_env
from work_agent.tools.models import PipelineHandle, PipelineResult


class RealPipelineTool:
    """
    接公司流水线时在此实现。

    建议映射（示例，以公司真实 API 为准）：
      company.create_job(...)  → create(...)  → 取返回的 pipeline_id
      company.start_job(id)    → start(pipeline_id)
      company.get_job(id)      → query(pipeline_id)

    不要把公司 parse_* / 拼写错误的函数名挂到 Protocol 上；
    映射只发生在本文件。
    """

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
        创建流水线。

        参数:
            case_names: 用例名列表。
            version: 版本。
            physical_env: 物理组网 IP；与逻辑模式互斥。
            logic_env: 规范逻辑组网名；须与 ``logic_constraint`` 成对。
            logic_constraint: 逻辑约束。
            options: 可选开关（如 ``debug_mode``）；接入公司 API 时摊平进请求体。

        返回:
            PipelineHandle（当前未实现）。
        """
        resolve_create_env(
            physical_env=physical_env,
            logic_env=logic_env,
            logic_constraint=logic_constraint,
        )
        raise NotImplementedError(
            "RealPipelineTool.create 未实现：请接入 external SDK 后在此映射"
        )

    def start(self, pipeline_id: str) -> bool:
        """
        启动流水线。

        参数:
            pipeline_id: 服务端 id。

        返回:
            是否成功（当前未实现）。
        """
        raise NotImplementedError(
            "RealPipelineTool.start 未实现：请接入 external SDK 后在此映射"
        )

    def query(self, pipeline_id: str) -> PipelineResult:
        """
        查询流水线。

        参数:
            pipeline_id: 服务端 id。

        返回:
            PipelineResult（当前未实现）。
        """
        raise NotImplementedError(
            "RealPipelineTool.query 未实现：请接入 external SDK 后在此映射"
        )
