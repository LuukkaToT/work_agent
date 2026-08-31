"""W3 MCP Client 与 RealKnowledgeSearchTool。"""

from work_agent.mcp.w3_client import (
    W3McpConfig,
    _pick_tool_name,
    search_via_w3_mcp,
)
from work_agent.tools.real.knowledge import RealKnowledgeSearchTool


def test_config_stdio_requires_command():
    cfg = W3McpConfig(transport="stdio", command="")
    assert cfg.is_configured() is False
    cfg2 = W3McpConfig(transport="stdio", command="python")
    assert cfg2.is_configured() is True


def test_config_http_requires_url():
    assert W3McpConfig(transport="http", url="").is_configured() is False
    assert W3McpConfig(transport="http", url="http://x/mcp").is_configured() is True


def test_pick_tool_name_prefers_exact_then_search():
    assert _pick_tool_name(["a", "w3_search", "b"], "w3_search") == "w3_search"
    assert _pick_tool_name(["foo_search_bar"], "missing") == "foo_search_bar"


def test_search_via_w3_unconfigured_message():
    out = search_via_w3_mcp(W3McpConfig(transport="stdio", command=""), "q")
    assert "not configured" in out


def test_real_knowledge_uses_injected_client():
    class Fake:
        def search(self, query: str, *, top_k: int = 3) -> str:
            return f"fake:{query}:{top_k}"

    tool = RealKnowledgeSearchTool(client=Fake())
    assert tool.search("CELL_BAND", top_k=2) == "fake:CELL_BAND:2"


def test_real_knowledge_empty_query():
    class Fake:
        def search(self, query: str, *, top_k: int = 3) -> str:
            return "should-not-run"

    assert "empty query" in RealKnowledgeSearchTool(client=Fake()).search("  ")


def test_real_knowledge_client_exception_degrades():
    class Boom:
        def search(self, query: str, *, top_k: int = 3) -> str:
            raise RuntimeError("down")

    out = RealKnowledgeSearchTool(client=Boom()).search("x")
    assert out.startswith("[knowledge/w3] error:")
    assert "down" in out
