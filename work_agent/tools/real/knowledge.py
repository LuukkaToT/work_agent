"""真实知识检索占位：对接内网 w3_search_tool MCP。"""

from __future__ import annotations


class RealKnowledgeSearchTool:
    """
    TODO: 通过 MCP Client 调用内网 w3_search_tool。
    诊断主路径不得强依赖本类成功。
    """

    def search(self, query: str, *, top_k: int = 3) -> str:
        """
        检索知识库。

        参数:
            query: 检索语句。
            top_k: 返回条数上限。

        返回:
            可读摘要（当前未实现）。
        """
        raise NotImplementedError(
            "RealKnowledgeSearchTool.search 未实现："
            "请接入内网 w3_search_tool MCP 后在此映射"
        )
