"""CL-05: MCP over stdio (docs/TEST_PLAN.md section 2).

Spawns ``th-cn mcp stdio`` with pipes and drives the raw JSON-RPC session:
initialize → initialized → tools/list → tools/call. Every stdout line must be
a JSON-RPC frame; operator logging stays on stderr.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tests.integration._procs import ROOT, SRC, child_env

pytestmark = pytest.mark.slow


class StdioSession:
    def __init__(self, cwd: Path):
        env = child_env()
        env["PYTHONPATH"] = str(SRC)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "tickhub_cn", "mcp", "stdio"],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.lines: list[str] = []
        self.queue: queue.Queue[str] = queue.Queue()
        self._stderr: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line)
            self.queue.put(line)

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr.append(line)

    def send(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def recv(self, *, timeout: float = 30.0) -> dict[str, Any]:
        line = self.queue.get(timeout=timeout)
        return json.loads(line)

    def response(self, request_id: int, *, timeout: float = 60.0) -> dict[str, Any]:
        """Return the frame for ``id``, tolerating server notifications."""
        import time

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no response for id={request_id}")
            msg = self.recv(timeout=remaining)
            if msg.get("id") == request_id:
                return msg

    def close(self) -> None:
        if self.proc.stdin is not None and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)

    @property
    def stderr_lines(self) -> list[str]:
        return list(self._stderr)


@pytest.fixture
def session(tmp_path: Path):
    sess = StdioSession(tmp_path)
    try:
        yield sess
    finally:
        sess.close()


def test_stdio_handshake_catalogue_and_call(session: StdioSession) -> None:
    session.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "tickhub-tests", "version": "0"},
            },
        }
    )
    init = session.response(1)
    info = init["result"]["serverInfo"]
    assert info["name"] == "tickhub-cn"
    assert info["version"]
    assert init["result"]["protocolVersion"]

    session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    session.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    listed = session.response(2)
    tools = listed["result"]["tools"]
    assert len(tools) == 146
    assert len({tool["name"] for tool in tools}) == 146
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object", tool["name"]
        assert tool["name"]
        assert tool.get("description")

    # A rule-based tool keeps the call offline and deterministic while still
    # exercising the full envelope/cache/provenance path.
    session.send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "apply_adjust",
                "arguments": {"code": "600000", "closes": "10,11", "factors": "2,2"},
            },
        }
    )
    called = session.response(3)
    result = called["result"]
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["data"] == {"code": "600000", "adjusted": [20.0, 22.0]}
    meta = payload["meta"]
    assert {"source", "fetched_at", "cache_hit", "confidence", "trace_id"} <= set(meta)
    assert len(meta["trace_id"]) == 16
    assert result["structuredContent"]["result"] == payload

    # Compatibility aliases are published alongside the canonical tools.
    assert {"tencent_kline", "get_kline"} <= {tool["name"] for tool in tools}

    # Field projection is applied inside the MCP tool just like in the CLI.
    session.send(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "apply_adjust",
                "arguments": {
                    "code": "600000",
                    "closes": "5",
                    "factors": "3",
                    "fields": "adjusted",
                },
            },
        }
    )
    projected = json.loads(session.response(4)["result"]["content"][0]["text"])
    assert projected["data"] == {"adjusted": [15.0]}

    session.close()

    assert session.proc.returncode == 0
    # Every stdout line is a JSON-RPC 2.0 frame — no banners, no log leakage.
    assert session.lines, "server produced no frames"
    for line in session.lines:
        frame = json.loads(line)
        assert frame["jsonrpc"] == "2.0"
    # Operator logging, when present, is JSON on stderr only.
    for line in session.stderr_lines:
        if line.strip():
            record = json.loads(line)
            assert "level" in record and "message" in record


def test_stdio_rejects_unknown_tool_without_touching_stdout(session: StdioSession) -> None:
    session.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "tickhub-tests", "version": "0"},
            },
        }
    )
    session.response(1)
    session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    session.send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        }
    )
    answer = session.response(2)
    assert "error" in answer or answer["result"].get("isError") is True
    session.close()
    for line in session.lines:
        assert json.loads(line)["jsonrpc"] == "2.0"
