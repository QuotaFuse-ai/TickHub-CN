"""QUOT-273: every CN data event carries the compliance marks FinanceCoffer gates on.

FinanceCoffer drops an event into its DLQ when ``source_info.compliance_tier`` is
not one of S/B/C/D/X or ``commercial_use`` is missing (``core/compliance.py``), so
the mark has to be on the envelope of every data type, not just the four original
ones. These tests build each event type the way ``crawler.store`` does on the live
path and read the envelope back after a JSON round trip, because that round trip
is exactly what the Redis Stream payload is.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Callable
from typing import Any

import pytest

from tickhub_cn.crawler import event_publisher, store

pytestmark = pytest.mark.contract

_CN_TZ = _dt.timezone(_dt.timedelta(hours=8))
_TIERS = frozenset({"S", "B", "C", "D", "X"})


def _data_events() -> dict[str, Callable[[], dict[str, Any]]]:
    return {
        "announcement": lambda: store._announcement_event(
            "cninfo",
            "ann-1",
            {"type": "回购", "title": "关于回购公司股份的公告"},
            code="600000",
            title="关于回购公司股份的公告",
            published="2026-10-20",
            url="http://static.cninfo.com.cn/t.pdf",
        ),
        "research": lambda: store._report_event(
            "eastmoney",
            {"org": "示例证券", "analyst": "张三", "rating": "买入", "pdf": "http://x/r.pdf"},
            code="600000",
            title="示例研报",
            published="2026-10-21",
            biz_key="R-1",
        ),
        "risk": lambda: store._risk_event(
            "cninfo",
            "risk-1",
            {"source": "szse", "severity": "high"},
            code="600000",
            risk_type="诉讼",
            occurred="2026-10-22",
            title="示例风险",
        ),
        "northbound": lambda: store._northbound_event(
            "2026-10-23", "hk2sh", {"name": "沪股通", "net_inflow": "12.5", "deal_amount_wan": "100"}
        ),
        "financial": lambda: store._financial_event(
            "income",
            {"revenue": 500.0, "net_profit": 100.0, "deducted_net_profit": 92.0},
            code="600000",
            report_date="2026-09-30",
            check=None,
        ),
        "kline": lambda: store._kline_event(
            "600000",
            {
                "date": "2026-10-23",
                "open": 10.3,
                "close": 10.5,
                "high": 10.6,
                "low": 10.2,
                "volume": 900000,
                "amount": 9400000.0,
            },
            period="day",
            adjust="qfq",
        ),
        "quote": lambda: store._quote_event(
            "600000",
            {"price": 10.5, "pe": 12.3, "pb": 1.1, "total_market_cap": 2500.0, "is_stale": False},
            trade_date="2026-10-23",
            stamp=_dt.datetime(2026, 10, 23, 15, 0, 3, tzinfo=_CN_TZ),
        ),
    }


def _serialized(event: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(event))


@pytest.mark.parametrize("data_type", sorted(_data_events()))
def test_every_data_event_carries_the_compliance_marks(data_type: str) -> None:
    event = _serialized(_data_events()[data_type]())

    assert event["data_type"] == data_type
    info = event["source_info"]
    assert info["compliance_tier"] in _TIERS
    assert info["compliance_tier"] == store.CN_COMPLIANCE_TIER
    assert info["commercial_use"] is False
    assert info["hub"] == store.CN_HUB
    # The provider stays: event_id is hashed from it, so the mark must be added
    # without disturbing the vendor label the archive was built under.
    assert info["provider"]


def test_market_events_use_the_same_constructor_as_the_original_four() -> None:
    """One source of the marks, so a new data type cannot forget them silently."""

    built = {name: _serialized(fn()) for name, fn in _data_events().items()}
    keys = {name: sorted(info["source_info"]) for name, info in built.items()}
    # announcement/research carry crawl_url; the rest are the plain mark set.
    plain = ["commercial_use", "compliance_tier", "crawl_time", "hub", "provider"]
    for data_type in ("risk", "northbound", "financial", "kline", "quote"):
        assert keys[data_type] == plain


@pytest.mark.parametrize("data_type", ["announcement", "research", "risk", "northbound"])
def test_original_event_types_keep_their_fields(data_type: str) -> None:
    """The fix is additive only: nothing these four already published moved."""

    event = _data_events()[data_type]()
    body = event["content"]["structured_fields"]
    assert "compliance_tier" not in body and "commercial_use" not in body
    assert event["market"] == "CN"
    assert event["schema_version"] == "1.0"
    assert event["event_time"].endswith("Z")


def test_heartbeat_stays_unmarked() -> None:
    """Service signals are not data events; FinanceCoffer gates only the latter."""

    event = event_publisher.build_event(
        provider="tickhub-cn",
        data_type="heartbeat",
        symbol=None,
        market="CN",
        biz_key="run-1",
        event_time=_dt.datetime(2026, 10, 23, 15, 0, tzinfo=_CN_TZ),
        content={"structured_fields": {"status": "ok"}},
    )
    assert "compliance_tier" not in event["source_info"]
    assert "commercial_use" not in event["source_info"]
