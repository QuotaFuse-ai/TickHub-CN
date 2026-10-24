"""CL-06: MCP over Streamable HTTP at ``/mcp`` (docs/TEST_PLAN.md section 2)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.integration._procs import JsonLog, free_port, port_open, spawn, wait_port

pytestmark = pytest.mark.slow


async def _drive(url: str) -> dict[str, Any]:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(trust_env=False, timeout=30.0) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                listed = await session.list_tools()
                called = await session.call_tool(
                    "apply_adjust",
                    {"code": "600000", "closes": "10,11", "factors": "2,2"},
                )
                return {
                    "server": init.server_info.name,
                    "version": init.server_info.version,
                    "tools": [tool.model_dump(by_alias=True) for tool in listed.tools],
                    "call": called.model_dump(by_alias=True),
                }


def test_http_mcp_serves_the_same_catalogue(tmp_path: Path) -> None:
    port = free_port()
    log = tmp_path / "http.log"
    proc = spawn(["mcp", "http", "--port", str(port)], cwd=tmp_path, log=log, LOG_LEVEL="info")
    try:
        wait_port(port)
        assert port_open(port)
        result = asyncio.run(_drive(f"http://127.0.0.1:{port}/mcp"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    assert result["server"] == "tickhub-cn"
    assert result["version"]
    names = {tool["name"] for tool in result["tools"]}
    assert len(names) == 149
    assert "get_kline" in names

    payload = json.loads(result["call"]["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["data"]["adjusted"] == [20.0, 22.0]
    assert {"source", "fetched_at", "cache_hit", "confidence", "trace_id"} <= set(payload["meta"])

    # The HTTP transport logs startup as structured JSON. Uvicorn's own access
    # lines are plain text (they go to stderr, not the protocol stream), so the
    # journal only needs to carry the structured startup event.
    journal = JsonLog(log)
    journal._read_new()
    starts = [row for row in journal.rows if row.get("event") == "mcp_start"]
    assert starts, journal.bad_lines
    assert starts[0]["detail"]["transport"] == "http"
    assert starts[0]["detail"]["tools"] == 149
