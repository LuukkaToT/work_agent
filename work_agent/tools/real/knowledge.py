"""真实知识检索：对接内网 w3_search_tool MCP（Client only）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from work_agent.core.config import get_settings
from work_agent.mcp.w3_client import (
    McpW3SearchClient,
    W3McpConfig,
    load_w3_mcp_config_from_env,
)

if TYPE_CHECKING:
    from work_agent.mcp.w3_client import W3SearchClient


class RealKnowledgeSearchTool:
    """
    通过 MCP Client 调用公司 w3_search。
    诊断主路径不得强依赖成功：异常/未配置时返回错误字符串。
    """

    def __init__(
        self,
        *,
        client: W3SearchClient | None = None,
        config: W3McpConfig | None = None,
    ) -> None:
        """
        参数:
            client: 可注入的检索客户端（单测 fake）；None 用 McpW3SearchClient。
            config: MCP 配置；None 从环境变量加载。
        """
        # 确保 .env 已加载
        get_settings()
        self._config = config if config is not None else load_w3_mcp_config_from_env()
        self._client = client if client is not None else McpW3SearchClient(self._config)

    def search(self, query: str, *, top_k: int = 3) -> str:
        """
        经 MCP 检索公司知识。

        参数:
            query: 检索语句。
            top_k: 条数上限。

        返回:
            可读摘要；空查询或异常时返回错误说明字符串。
        """
        q = (query or "").strip()
        if not q:
            return "[knowledge/w3] empty query"
        try:
            return self._client.search(q, top_k=top_k)
        except Exception as exc:  # noqa: BLE001
            return f"[knowledge/w3] error: {exc}"
