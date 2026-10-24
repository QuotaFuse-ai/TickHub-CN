"""Scratch probe: drive the stdio transport with raw JSON-RPC frames."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

PY = sys.executable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["PYTHONIOENCODING"] = "utf-8"
    env["LOG_LEVEL"] = "info"
    env["REDIS_URL"] = "redis://127.0.0.1:6399/0"
    proc = subprocess.Popen(
        [PY, "-m", "tickhub_cn", "mcp", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put("")

    threading.Thread(target=pump, daemon=True).start()

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv(timeout: float = 20.0) -> dict:
        line = lines.get(timeout=timeout)
        if not line:
            raise RuntimeError("stdout closed")
        return json.loads(line)

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"},
    }})
    print("INIT:", json.dumps(recv(), ensure_ascii=False)[:600])
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    listed = recv()
    tools = listed.get("result", {}).get("tools", [])
    print("TOOLS:", len(tools), "sample keys:", sorted(tools[0]) if tools else None)
    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "tencent_quote", "arguments": {"codes": "600000"},
    }})
    called = recv()
    print("CALL keys:", sorted(called.get("result", {})))
    content = called.get("result", {}).get("content") or []
    print("CONTENT:", json.dumps(content, ensure_ascii=False)[:400])

    send({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    # there may be server-initiated notifications; skip until id 4
    while True:
        msg = recv()
        if msg.get("id") == 4:
            print("SECOND LIST:", len(msg.get("result", {}).get("tools", [])))
            break
        print("EXTRA FRAME:", json.dumps(msg)[:200])

    assert proc.stdin is not None
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    err = proc.stderr.read() if proc.stderr else ""
    print("EXIT:", proc.returncode)
    print("STDERR tail:", err[-800:])


if __name__ == "__main__":
    main()
