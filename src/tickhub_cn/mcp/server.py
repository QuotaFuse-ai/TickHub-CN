"""MCP surface for TickHub-CN.

Every registered data function is exposed as one MCP tool, plus the
compatibility aliases the original a-stock-data client names rely on. The
server carries no model binding: any MCP-capable client can attach to it over
stdio or Streamable HTTP.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver import MCPServer

from .. import __version__
from ..core.config import get_settings
from ..logging.setup import get_logger

INSTRUCTIONS = """\
TickHub-CN serves A-share research data as structured tools.

Rules for callers:
- All prices, financials, fund flows and holdings MUST come from these tools.
  Never infer or fabricate a numeric value.
- Every response is an envelope: {"data": ..., "meta": {source, fetched_at,
  cache_hit, confidence, trace_id}}. Quote meta.source when citing data.
- Optional keyword arguments on every tool:
  fields="a,b"    project the response to those fields
  data_as_of=ISO  point-in-time view: drop rows published after that moment
  _refresh=true   bypass cache and refetch
  _no_cache=true  skip cache for this call
- Call `doctor`-style diagnostics from the CLI (th-cn doctor) when a source
  looks stale rather than retrying blindly.
"""


def build_server(*, include_aliases: bool = True, log_level: str | None = None) -> MCPServer:
    """Build the MCP server with every registered tool."""
    import tickhub_cn.datasources  # noqa: F401  (registers all tools)

    from ..datasources.base import ALIASES, REGISTRY

    settings = get_settings()
    server = MCPServer(
        name="tickhub-cn",
        title="TickHub-CN",
        version=__version__,
        instructions=INSTRUCTIONS,
        log_level=(log_level or settings.log_level).upper(),
    )
    for name in sorted(REGISTRY):
        spec = REGISTRY[name]
        server.add_tool(spec.func, name=name, description=spec.description or name)
    if include_aliases:
        for alias in sorted(ALIASES):
            target = ALIASES[alias]
            spec = REGISTRY[target]
            server.add_tool(
                spec.func,
                name=alias,
                description=f"{spec.description or target} (compatibility alias of {target})",
            )
    return server


def tool_count(*, include_aliases: bool = True) -> int:
    import tickhub_cn.datasources  # noqa: F401

    from ..datasources.base import ALIASES, REGISTRY

    return len(REGISTRY) + (len(ALIASES) if include_aliases else 0)


def run_stdio() -> None:
    """Serve MCP over stdio. stdout carries protocol frames only."""
    settings = get_settings()
    # stderr logging is mandatory here: anything on stdout corrupts the session.
    from ..logging.setup import setup_logging

    setup_logging(settings, force=True)
    get_logger("mcp").info(
        "stdio server starting",
        extra={"event": "mcp_start", "detail": {"transport": "stdio", "tools": tool_count()}},
    )
    asyncio.run(build_server().run_stdio_async())


def run_http(host: str = "127.0.0.1", port: int = 8000, path: str = "/mcp") -> None:
    """Serve MCP over Streamable HTTP at ``path``."""
    server = build_server()
    get_logger("mcp").info(
        "http server starting",
        extra={
            "event": "mcp_start",
            "detail": {"transport": "http", "host": host, "port": port, "path": path, "tools": tool_count()},
        },
    )
    asyncio.run(
        server.run_streamable_http_async(host=host, port=port, streamable_http_path=path)
    )


def create_app(host: str = "127.0.0.1", path: str = "/mcp") -> Any:
    """Starlette app for embedding (tests, docker, custom health routes)."""
    return build_server().streamable_http_app(host=host, streamable_http_path=path)


__all__ = ["INSTRUCTIONS", "build_server", "create_app", "run_http", "run_stdio", "tool_count"]
