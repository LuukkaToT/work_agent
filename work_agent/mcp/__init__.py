"""MCP 相关适配（当前仅公司 w3_search Client）。"""

from work_agent.mcp.w3_client import (
    McpW3SearchClient,
    W3McpConfig,
    load_w3_mcp_config_from_env,
    search_via_w3_mcp,
)

__all__ = [
    "McpW3SearchClient",
    "W3McpConfig",
    "load_w3_mcp_config_from_env",
    "search_via_w3_mcp",
]
