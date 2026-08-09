"""真实日志适配层占位。"""

from __future__ import annotations


class RealLogTool:
    def fetch_logs(self, pipeline_id: str) -> str:
        raise NotImplementedError(
            "RealLogTool.fetch_logs 未实现：请接入 external SDK 后在此映射"
        )
