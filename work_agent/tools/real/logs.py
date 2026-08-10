"""真实日志适配层占位。"""

from __future__ import annotations


class RealLogTool:
    """LogTool 真实实现占位（接入 external SDK 后映射）。"""

    def fetch_logs(
        self,
        pipeline_id: str,
        *,
        tail_lines: int | None = 200,
    ) -> str:
        """
        拉取日志。

        参数:
            pipeline_id: 流水线 id。
            tail_lines: None 全文，否则尾部 N 行。

        返回:
            日志文本（当前未实现）。
        """
        raise NotImplementedError(
            "RealLogTool.fetch_logs 未实现：请接入 external SDK 后在此映射"
        )

    def grep_logs(
        self,
        pipeline_id: str,
        pattern: str,
        *,
        context_lines: int = 3,
        max_matches: int = 20,
    ) -> str:
        """
        检索日志。

        参数:
            pipeline_id: 流水线 id。
            pattern: 检索模式。
            context_lines: 上下文行数。
            max_matches: 最多命中数。

        返回:
            检索结果文本（当前未实现）。
        """
        raise NotImplementedError(
            "RealLogTool.grep_logs 未实现：请接入 external SDK 后在此映射"
        )
