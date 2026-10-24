"""UI-01/UI-02: the browser surface is a gateway, never a direct MCP client."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

WEB = Path(__file__).resolve().parents[2] / "web"


def _gateway() -> Any:
    spec = importlib.util.spec_from_file_location("tickhub_web_gateway", WEB / "gateway.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENVELOPE = {
    "ok": True,
    "data": [{"code": "600000", "price": 10.5}],
    "meta": {
        "source": "tencent",
        "fetched_at": "2026-10-24T12:00:00+08:00",
        "cache_hit": False,
        "confidence": "A",
        "trace_id": "0123456789abcdef",
    },
}


def test_gateway_serves_page_and_untouched_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    gateway = _gateway()
    monkeypatch.setattr(gateway.client, "tencent_quote", lambda codes: ENVELOPE)
    client = TestClient(gateway.app)

    page = client.get("/")
    assert page.status_code == 200
    assert "TickHub-CN" in page.text

    assert client.get("/healthz").json() == {"ok": True}

    quoted = client.get("/api/quote", params={"codes": "600000"})
    assert quoted.status_code == 200
    assert quoted.json() == ENVELOPE

    # A blank code is rejected by the gateway itself, in envelope shape.
    blank = client.get("/api/quote", params={"codes": " "})
    assert blank.status_code == 400
    assert blank.json()["ok"] is False
    assert blank.json()["error"]["code"] == "QFSD-9001"

    # Query validation happens before the client is touched.
    assert client.get("/api/kline", params={"code": "600000", "period": "hourly"}).status_code == 422


def test_page_explains_gateway_and_never_calls_mcp() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    script = "\n".join(re.findall(r"<script>(.*?)</script>", html, flags=re.S))
    assert "fetch(" in script
    assert "/mcp" not in script
    assert "jsonrpc" not in script.lower()

    prose = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
    assert "JSON-RPC" in prose
    assert "SDK" in prose
    assert "gateway.py" in prose
