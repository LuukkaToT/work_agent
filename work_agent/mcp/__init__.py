"""MCP Client 与 Testing Agent Server 适配。"""

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
