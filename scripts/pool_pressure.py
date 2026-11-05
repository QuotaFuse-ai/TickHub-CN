"""Pressure-probe the limit-up pool tools over MCP Streamable HTTP.

Reproduces the LadderStock failure mode described in QUOT-359: several callers
hit the *same* push2ex endpoint family inside its 25 s pace window. Each call is
a real MCP ``tools/call``; nothing is mocked.

Examples
--------
Burst (the production failure mode — same args, different callers)::

    python scripts/pool_pressure.py --url http://127.0.0.1:8000/mcp \
        --tool limit_up_pool --dates 20261028 --concurrency 4

Sequential calls inside the pace window (different dates = different cache
keys, same endpoint family)::

    python scripts/pool_pressure.py --url http://127.0.0.1:8010/mcp \
        --tool limit_up_pool --dates 20261029,20261030 --gap 0.2

Output is JSON: one record per call plus a summary. ``qfsd_7001`` counts the
paced/rejected calls; ``stale`` counts degraded replays (``meta.degraded``).
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

DEFAULT_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}
_ERROR_RE = re.compile(r"QFSD-\d{4}")


class McpClient:
    """Minimal synchronous MCP Streamable HTTP client (initialize + tools/call)."""

    def __init__(self, url: str, timeout: float = 15.0) -> None:
        self.url = url
        self.timeout = timeout
        self.http = requests.Session()
        self.session_id: str | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._initialize()

    def _post(self, body: dict[str, Any], *, notify: bool = False) -> dict[str, Any]:
        headers = dict(DEFAULT_HEADERS)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        resp = self.http.post(self.url, json=body, headers=headers, timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if notify:
            return {}
        if not self.session_id:
            self.session_id = resp.headers.get("Mcp-Session-Id")
        if "text/event-stream" in resp.headers.get("Content-Type", ""):
            # Decode explicitly as UTF-8: the SSE Content-Type often has no
            # charset, and requests' latin-1 fallback turns multi-byte text into
            # C1 control codes that ``splitlines`` mistakes for line breaks.
            raw = resp.content.decode("utf-8", "replace")
            data: list[str] = []
            for line in raw.split("\n"):
                line = line.rstrip("\r")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
                elif data:
                    break
            if not data:
                raise RuntimeError("empty SSE response")
            return json.loads("\n".join(data))
        return resp.json()

    def _initialize(self) -> None:
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "quot359-pressure", "version": "1"},
            },
        }
        self._post(body)
        if self.session_id:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notify=True)

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            req_id = next(self._ids)
        body = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
        payload = self._post(body)
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        result = payload.get("result") or {}
        if result.get("isError"):
            text = " ".join(
                part.get("text", "") for part in result.get("content") or [] if isinstance(part, dict)
            )
            return {"is_error": True, "error": text or "unknown", "code": _code(text)}
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            # The SDK wraps a dict return as {"result": {...}}.
            if set(structured) == {"result"} and isinstance(structured["result"], dict):
                structured = structured["result"]
            return {"is_error": False, "payload": structured}
        for part in result.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                try:
                    return {"is_error": False, "payload": json.loads(part["text"])}
                except json.JSONDecodeError:
                    break
        return {"is_error": True, "error": "unparseable tool result", "code": None}


def _code(text: str) -> str | None:
    match = _ERROR_RE.search(text or "")
    return match.group(0) if match else None


def _observe(tool: str, args: dict[str, Any], result: dict[str, Any], elapsed: float) -> dict[str, Any]:
    meta = ((result.get("payload") or {}).get("meta") or {}) if not result.get("is_error") else {}
    rows = (result.get("payload") or {}).get("data") if not result.get("is_error") else None
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("pool") or rows
    return {
        "tool": tool,
        "args": args,
        "ok": not result.get("is_error"),
        "code": result.get("code"),
        "error": (result.get("error") or "")[:160] or None,
        "rows": len(rows) if isinstance(rows, list) else None,
        "cache_hit": meta.get("cache_hit"),
        "degraded": meta.get("degraded"),
        "stale_age_s": meta.get("stale_age_s"),
        "retry_after_s": meta.get("retry_after_s"),
        "elapsed_ms": round(elapsed * 1000),
    }


def _run_one(client: McpClient, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = client.call(tool, args)
    except Exception as exc:  # noqa: BLE001 - the probe reports, it does not retry
        result = {"is_error": True, "error": str(exc)[:160], "code": _code(str(exc))}
    return _observe(tool, args, result, time.monotonic() - started)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--tool", default="limit_up_pool")
    parser.add_argument("--dates", required=True, help="Comma-separated YYYYMMDD values.")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat the date list N times.")
    parser.add_argument("--concurrency", type=int, default=1, help="Parallel calls (1 = sequential).")
    parser.add_argument("--gap", type=float, default=0.0, help="Seconds between sequential calls.")
    parser.add_argument("--refresh", action="store_true", help="Pass refresh=true (force upstream).")
    parser.add_argument("--no-cache", action="store_true", help="Pass no_cache=true (skip cache entirely).")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    if not dates:
        parser.error("--dates is empty")
    controls: dict[str, Any] = {}
    if args.refresh:
        controls["refresh"] = True
    if args.no_cache:
        controls["no_cache"] = True
    calls = [{**{"date": d}, **controls} for d in dates] * max(1, args.repeat)
    client = McpClient(args.url, timeout=args.timeout)

    started = time.monotonic()
    records: list[dict[str, Any]] = []
    if args.concurrency > 1:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(_run_one, client, args.tool, spec) for spec in calls]
            for future in futures:
                records.append(future.result())
    else:
        for index, spec in enumerate(calls):
            if index and args.gap:
                time.sleep(args.gap)
            records.append(_run_one(client, args.tool, spec))

    summary = {
        "url": args.url,
        "tool": args.tool,
        "calls": len(records),
        "ok": sum(1 for r in records if r["ok"]),
        "failed": sum(1 for r in records if not r["ok"]),
        "qfsd_7001": sum(1 for r in records if r["code"] == "QFSD-7001"),
        "stale": sum(1 for r in records if r["degraded"] == "stale_cache"),
        "elapsed_s": round(time.monotonic() - started, 2),
    }
    json.dump({"summary": summary, "calls": records}, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    # Non-zero exit when the probe saw the failure it exists to detect.
    return 0 if summary["qfsd_7001"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
