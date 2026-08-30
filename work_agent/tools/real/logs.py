"""真实日志适配层占位。"""

from __future__ import annotations


class RealLogTool:
    """LogTool 真实实现占位（接入 external SDK 后映射）。"""

    def list_logs(self, pipeline_id: str) -> str:
        """列出流水线可用日志文件（当前未实现）。"""
        raise NotImplementedError(
            "RealLogTool.list_logs 未实现：请接入 external SDK 后在此映射"
        )

    def fetch_logs(
        self,
        pipeline_id: str,
        *,
        tail_lines: int | None = 200,
        component: str | None = None,
    ) -> str:
        """
        拉取日志。

        参数:
            pipeline_id: 流水线 id。
            tail_lines: None 全文，否则尾部 N 行。
            component: 可选组件名；None 为合并时间线。

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
        component: str | None = None,
    ) -> str:
        """
        检索日志。

        参数:
            pipeline_id: 流水线 id。
            pattern: 检索模式。
            context_lines: 上下文行数。
            max_matches: 最多命中数。
            component: 可选组件名；None 检索全部日志。

        返回:
            检索结果文本（当前未实现）。
        """
        raise NotImplementedError(
            "RealLogTool.grep_logs 未实现：请接入 external SDK 后在此映射"
        )

    def lookup_error_code(self, code: str) -> str:
        """查询错误码目录（当前未实现）。"""
        raise NotImplementedError(
            "RealLogTool.lookup_error_code 未实现：请接入公司错误码目录后在此映射"
        )
