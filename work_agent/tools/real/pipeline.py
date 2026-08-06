"""真实流水线适配层：映射 external SDK → PipelineTool Protocol。"""

from __future__ import annotations

from work_agent.tools.models import PipelineHandle, PipelineResult


class RealPipelineTool:
    """
    接真实流水线时在此实现。

    建议映射（示例，以真实 API 为准）：
      client.create_job(...)  → create(...)  → 取返回的 pipeline_id
      client.start_job(id)    → start(pipeline_id)
      client.get_job(id)      → query(pipeline_id)

    不要把真实 SDK 的 parse_* / 拼写错误函数名挂到 Protocol 上；
    映射只发生在本文件。
    """

    def create(
        self,
        case_names: list[str],
        version: str,
        env: str,
    ) -> PipelineHandle:
        raise NotImplementedError(
            "RealPipelineTool.create 未实现：请接入 external SDK 后在此映射"
        )

    def start(self, pipeline_id: str) -> bool:
        raise NotImplementedError(
            "RealPipelineTool.start 未实现：请接入 external SDK 后在此映射"
        )

    def query(self, pipeline_id: str) -> PipelineResult:
        raise NotImplementedError(
            "RealPipelineTool.query 未实现：请接入 external SDK 后在此映射"
        )
