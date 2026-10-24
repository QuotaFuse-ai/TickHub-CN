"""Thin read-only gateway for the browser.

MCP Streamable HTTP is JSON-RPC, not REST: a page cannot call ``/mcp`` with
``fetch()``. This gateway exposes a small, fixed allow-list of reads over plain
HTTP and forwards the untouched TickHub-CN envelope.

It runs in the same process as the Python client, so it does not need the MCP
server at all. Keep it on loopback; it has no authentication.

    python web/gateway.py --port 8100

Then open http://127.0.0.1:8100/.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from tickhub_cn import StockDataClient
from tickhub_cn.core.errors import TickHubError

HERE = Path(__file__).resolve().parent
client = StockDataClient()

app = FastAPI(
    title="TickHub-CN read-only gateway",
    version="0.1.0",
    description=(
        "Browser-facing adapter over a fixed allow-list of TickHub-CN reads. "
        "MCP-HTTP itself is JSON-RPC and must not be called directly from a page."
    ),
)


def _envelope(exc: TickHubError) -> JSONResponse:
    status = {
        "QFSD-9001": 400,
        "QFSD-9002": 404,
        "QFSD-9005": 404,
        "QFSD-6001": 401,
    }.get(exc.code, 503)
    return JSONResponse(status_code=status, content={"ok": False, "error": exc.to_dict()})


def _code(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise TickHubError("QFSD-9001", message="code is required")
    return raw


@app.exception_handler(TickHubError)
def _tool_error(_request: Any, exc: TickHubError) -> JSONResponse:
    return _envelope(exc)


@app.get("/", include_in_schema=False)
def index() -> Any:
    page = HERE / "index.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="index.html is missing")
    return FileResponse(page)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/quote")
def quote(codes: str = Query(..., description="Comma-separated A-share codes.")) -> Any:
    return client.tencent_quote(_code(codes))


@app.get("/api/profile")
def profile(code: str = Query(...)) -> Any:
    return client.stock_profile(_code(code))


@app.get("/api/kline")
def kline(
    code: str = Query(...),
    period: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
    adjust: str = Query("qfq", pattern="^(qfq|hfq|none)$"),
    limit: int = Query(30, ge=1, le=500),
) -> Any:
    return client.tencent_kline(_code(code), period=period, adjust=adjust, limit=limit)


@app.get("/api/crosscheck")
def crosscheck(code: str = Query(...)) -> Any:
    return client.finance_crosscheck(_code(code))


@app.get("/api/announcements")
def announcements(code: str = Query(...), limit: int = Query(10, ge=1, le=50)) -> Any:
    return client.cninfo_announcements(_code(code), limit=limit)


@app.get("/api/valuation")
def valuation(code: str = Query(...)) -> Any:
    return client.full_valuation(_code(code))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TickHub-CN read-only browser gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "::1", "localhost") and os.environ.get("TICKHUB_GATEWAY_ALLOW_REMOTE") != "1":
        sys.stderr.write(
            "refusing to bind a non-loopback host without TICKHUB_GATEWAY_ALLOW_REMOTE=1: "
            "this gateway has no authentication\n"
        )
        return 3

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
