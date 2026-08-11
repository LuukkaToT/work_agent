"""
公司 w3_search MCP Client（只读检索）。

支持:
  - stdio：拉起本地/内网 MCP 进程
  - http：streamable HTTP（内网共享 endpoint）

失败返回可读错误字符串，不向诊断主路径抛异常。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import shlex
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class W3McpConfig:
    """从环境变量加载的 W3 MCP 连接配置。"""

    transport: str = "stdio"  # stdio | http
    command: str = ""
    args: tuple[str, ...] = ()
    url: str = ""
    tool_name: str = "w3_search"
    query_arg: str = "query"
    top_k_arg: str = "top_k"
    timeout_seconds: float = 60.0

    def is_configured(self) -> bool:
        """
        判断当前配置是否足以发起连接。

        返回:
            stdio 需有 command；http 需有 url；否则 False。
        """
        t = (self.transport or "").strip().lower()
        if t == "stdio":
            return bool(self.command.strip())
        if t in ("http", "streamable-http", "streamable_http"):
            return bool(self.url.strip())
        return False


def load_w3_mcp_config_from_env() -> W3McpConfig:
    """
    从环境变量构造 W3McpConfig（调用方应已 load_dotenv）。

    返回:
        解析后的配置；缺省项用类字段默认值。
    """
    import os

    raw_args = os.getenv("W3_MCP_ARGS", "").strip()
    if raw_args:
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, list):
                args = tuple(str(a) for a in parsed)
            else:
                args = tuple(shlex.split(raw_args))
        except json.JSONDecodeError:
            args = tuple(shlex.split(raw_args))
    else:
        args = ()

    return W3McpConfig(
        transport=os.getenv("W3_MCP_TRANSPORT", "stdio").strip() or "stdio",
        command=os.getenv("W3_MCP_COMMAND", "").strip(),
        args=args,
        url=os.getenv("W3_MCP_URL", "").strip(),
        tool_name=os.getenv("W3_MCP_TOOL_NAME", "w3_search").strip() or "w3_search",
        query_arg=os.getenv("W3_MCP_QUERY_ARG", "query").strip() or "query",
        top_k_arg=os.getenv("W3_MCP_TOP_K_ARG", "top_k").strip() or "top_k",
        timeout_seconds=float(os.getenv("W3_MCP_TIMEOUT", "60")),
    )


class W3SearchClient(Protocol):
    """可注入的检索客户端（单测用 fake）。"""

    def search(self, query: str, *, top_k: int = 3) -> str:
        """
        执行检索并返回可读文本。

        参数:
            query: 检索语句。
            top_k: 条数上限。

        返回:
            摘要字符串（失败时也返回错误说明，不抛异常为佳）。
        """
        ...


def _content_to_text(result: Any) -> str:
    """把 MCP CallToolResult 压成字符串。"""
    parts: list[str] = []
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
        else:
            parts.append(str(block))
    structured = getattr(result, "structuredContent", None)
    if structured is not None and not parts:
        try:
            parts.append(json.dumps(structured, ensure_ascii=False, indent=2))
        except TypeError:
            parts.append(str(structured))
    is_error = getattr(result, "isError", False)
    text = "\n".join(parts).strip() or str(result)
    if is_error:
        return f"[knowledge/w3] tool error: {text}"
    return text


def _pick_tool_name(listed: list[str], preferred: str) -> str:
    """从 list_tools 结果里挑出最合适的检索 tool 名。"""
    if preferred in listed:
        return preferred
    for name in listed:
        low = name.lower()
        if "w3" in low and "search" in low:
            return name
    for name in listed:
        if "search" in name.lower():
            return name
    return preferred


async def _search_async(cfg: W3McpConfig, query: str, top_k: int) -> str:
    """在异步 MCP 会话中执行一次 search。"""
    from mcp import ClientSession

    transport = (cfg.transport or "stdio").strip().lower()

    async def _run_session(read: Any, write: Any) -> str:
        """初始化会话、选 tool、call_tool 并格式化结果。"""
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_resp = await session.list_tools()
            names = [t.name for t in (tools_resp.tools or [])]
            tool_name = _pick_tool_name(names, cfg.tool_name)
            arguments: dict[str, Any] = {cfg.query_arg: query}
            if cfg.top_k_arg:
                arguments[cfg.top_k_arg] = top_k
            result = await session.call_tool(tool_name, arguments=arguments)
            body = _content_to_text(result)
            header = (
                f"[knowledge/w3] tool={tool_name} transport={transport} "
                f"discovered={len(names)}"
            )
            return f"{header}\n{body}"

    if transport == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=cfg.command,
            args=list(cfg.args),
        )
        async with stdio_client(params) as (read, write):
            return await asyncio.wait_for(
                _run_session(read, write),
                timeout=cfg.timeout_seconds,
            )

    if transport in ("http", "streamable-http", "streamable_http"):
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(cfg.url) as (read, write):
            return await asyncio.wait_for(
                _run_session(read, write),
                timeout=cfg.timeout_seconds,
            )

    raise ValueError(f"unsupported W3_MCP_TRANSPORT={cfg.transport!r}")


def search_via_w3_mcp(cfg: W3McpConfig, query: str, *, top_k: int = 3) -> str:
    """
    同步入口：在独立事件循环中跑 MCP 调用。

    参数:
        cfg: MCP 连接配置。
        query: 检索语句。
        top_k: 条数上限。

    返回:
        可读检索文本；未配置或异常时返回错误说明字符串。
    """
    if not cfg.is_configured():
        return (
            "[knowledge/w3] not configured: set W3_MCP_TRANSPORT and "
            "W3_MCP_COMMAND (stdio) or W3_MCP_URL (http)"
        )

    def _run() -> str:
        """在新事件循环中执行 _search_async。"""
        return asyncio.run(_search_async(cfg, query, top_k))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return _run()
        except Exception as exc:  # noqa: BLE001
            return f"[knowledge/w3] error: {exc}"

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_run).result(timeout=cfg.timeout_seconds + 5)
    except Exception as exc:  # noqa: BLE001
        return f"[knowledge/w3] error: {exc}"


@dataclass
class McpW3SearchClient:
    """默认 Client：每次 search 短连接（简单、无长期子进程状态）。"""

    config: W3McpConfig = field(default_factory=load_w3_mcp_config_from_env)

    def search(self, query: str, *, top_k: int = 3) -> str:
        """
        按配置调用 w3_search。

        参数:
            query: 检索语句。
            top_k: 条数上限。

        返回:
            可读文本（含失败时的错误说明）。
        """
        return search_via_w3_mcp(self.config, query, top_k=top_k)
