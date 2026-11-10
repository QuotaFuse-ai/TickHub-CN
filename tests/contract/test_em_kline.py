"""CT-EM-KLINE: Eastmoney push2his K-line (QUOT-501) — secid, qfq flag, row shape."""

from __future__ import annotations

from typing import Any

import pytest

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import quotes

_KLINES = [
    "2021-11-10,6.65,6.60,6.65,6.56,274789,232796692.00,1.35,-0.90,-0.06,0.09",
    "2021-11-11,6.60,6.74,6.74,6.59,314193,269103088.00,2.27,2.12,0.14,0.11",
]


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_em_get(url: str, params: dict[str, Any] | None = None, **kwargs: Any):
        calls.append((url, dict(params or {})))
        return {"rc": 0, "data": {"code": "600000", "name": "浦发银行", "klines": list(_KLINES)}}

    monkeypatch.setattr(quotes, "em_get", fake_em_get)
    return calls


def test_em_kline_requests_qfq_push2his(upstream: list[tuple[str, dict[str, Any]]]) -> None:
    payload = quotes.em_kline(
        "600000", limit=2000, start="2021-11-10", end="2026-11-10", _no_cache=True
    )

    assert payload["ok"] is True
    assert payload["meta"]["source"] == "eastmoney"
    rows = payload["data"]["klines"]
    assert len(rows) == 2
    assert rows[0] == {
        "date": "2021-11-10",
        "open": 6.65,
        "close": 6.60,
        "high": 6.65,
        "low": 6.56,
        "volume": 274789.0,
        "amount": 232796692.0,
    }

    url, params = upstream[0]
    assert url == quotes._EM_KLINE
    assert params["secid"] == "1.600000"
    assert params["klt"] == 101 and params["fqt"] == 1
    assert params["beg"] == "20211110" and params["end"] == "20261110"
    assert params["lmt"] == 2000


@pytest.mark.parametrize(
    ("code", "secid"),
    [("sh000001", "1.000001"), ("000001", "0.000001"), ("399001", "0.399001")],
)
def test_em_kline_maps_market_to_secid(
    upstream: list[tuple[str, dict[str, Any]]], code: str, secid: str
) -> None:
    quotes.em_kline(code, _no_cache=True)
    assert upstream[0][1]["secid"] == secid


def test_em_kline_rejects_unknown_period_or_adjust(
    upstream: list[tuple[str, dict[str, Any]]],
) -> None:
    with pytest.raises(TickHubError):
        quotes.em_kline("600000", period="m5", _no_cache=True)
    with pytest.raises(TickHubError):
        quotes.em_kline("600000", adjust="split", _no_cache=True)
    assert upstream == []
