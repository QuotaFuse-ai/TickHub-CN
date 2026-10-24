"""IT-01..IT-10: opt-in live smoke, one representative tool per layer.

Run with ``TICKHUB_LIVE=1 pytest -m live``. Every case accepts either a real
envelope or a structured source error - networks block vendors, and a blocked
host must degrade, never crash. Values are never asserted (markets move);
only shapes, types and provenance are.
"""

from __future__ import annotations

import os

import pytest

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import LAYERS, call_tool

LIVE = os.environ.get("TICKHUB_LIVE", "0").strip().lower() in ("1", "true", "on", "yes")

CASES = [
    ("quotes", "tencent_quote", {"codes": "600000"}),
    ("financials", "eastmoney_finance_indicators", {"code": "600000", "periods": 4}),
    ("announcements", "cninfo_announcements", {"code": "600000", "limit": 5}),
    ("research", "eastmoney_reports", {"code": "600000", "page_size": 5}),
    ("market", "industry_boards", {"page_size": 5}),
    ("macro", "china_gdp", {"page_size": 5}),
    ("funds", "stock_fund_flow", {"code": "600000", "days": 5}),
    ("index", "index_kline", {"code": "000300", "days": 5}),
    ("bond", "convertible_bonds", {}),
    ("event", "earnings_forecast", {"code": "600000", "limit": 5}),
    ("futures", "futures_daily", {}),
    ("sentiment", "cninfo_irm", {"code": "000001", "page_size": 5}),
    ("option", "sina_option_codes", {"underlying": "510050"}),
    ("valuation", "full_valuation", {"code": "600000"}),
]


def test_every_layer_has_a_live_case() -> None:
    covered = [layer for layer, _, _ in CASES]
    assert sorted(covered) == sorted(LAYERS), "live suite drifted from the layer registry"
    assert len(covered) == len(set(covered))


@pytest.mark.live
@pytest.mark.skipif(not LIVE, reason="set TICKHUB_LIVE=1 to hit the public data sources")
def test_nf04_batch_of_fifty_quotes_is_fast() -> None:
    import time

    codes = ",".join(f"{600000 + i:06d}" for i in range(50))
    started = time.perf_counter()
    try:
        payload = call_tool("tencent_quote_batch", {"codes": codes})
    except TickHubError as exc:
        pytest.skip(f"source unavailable: {exc.code} {exc.message}")
    elapsed = time.perf_counter() - started
    data = payload.get("data") or {}
    if not data.get("succeeded"):
        pytest.skip("source returned no rows; cannot time a healthy batch")
    assert data["total"] == 50
    assert elapsed < 10.0, f"50-quote batch took {elapsed:.1f}s"


@pytest.mark.live
@pytest.mark.skipif(not LIVE, reason="set TICKHUB_LIVE=1 to hit the public data sources")
@pytest.mark.parametrize(("layer", "tool", "params"), CASES, ids=[c[0] for c in CASES])
def test_representative_tool_degrades_or_returns_shape(layer: str, tool: str, params: dict) -> None:
    try:
        payload = call_tool(tool, params)
    except TickHubError as exc:
        # A blocked host must surface a structured error, not an exception leak.
        assert exc.code.startswith("QFSD-") and len(exc.code) == 9
        assert exc.message
        return

    assert isinstance(payload, dict), layer
    if payload.get("ok") is False:
        error = payload.get("error") or {}
        assert str(error.get("code", "")).startswith("QFSD-"), payload
        return

    assert payload.get("ok") is True, payload
    assert "data" in payload
    meta = payload.get("meta") or {}
    assert meta.get("source"), payload
    assert meta.get("fetched_at"), payload
    assert meta.get("confidence") in ("A", "B", "C"), payload
    trace = str(meta.get("trace_id") or "")
    assert len(trace) == 16, payload
