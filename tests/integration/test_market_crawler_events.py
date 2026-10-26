"""T-102: financials / kline / quote archive their rows and publish each new one once."""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

import pytest

from tickhub_cn.crawler import event_publisher
from tickhub_cn.crawler.jobs import run_task
from tickhub_cn.crawler.store import get_archive

pytestmark = pytest.mark.slow

_CN_TZ = _dt.timezone(_dt.timedelta(hours=8))

FINANCIALS = {
    "balance": [
        {"report_date": "2026-09-30", "assets": 1000.0, "liabilities": 600.0, "equity": 400.0},
        {"report_date": "2026-06-30", "assets": 900.0, "liabilities": 550.0, "equity": 350.0},
    ],
    "income": [
        {
            "report_date": "2026-09-30",
            "revenue": 500.0,
            "total_profit": 120.0,
            "income_tax": 20.0,
            "net_profit": 100.0,
        },
        {
            "report_date": "2026-06-30",
            "revenue": 400.0,
            "total_profit": 90.0,
            "income_tax": 10.0,
            "net_profit": 80.0,
        },
    ],
    "cashflow": [
        {
            "report_date": "2026-09-30",
            "ending_cash": 1000.0,
            "beginning_cash": 800.0,
            "net_cash_increase": 200.0,
        },
        {
            "report_date": "2026-06-30",
            "ending_cash": 800.0,
            "beginning_cash": 700.0,
            "net_cash_increase": 100.0,
        },
    ],
}

KLINES = [
    {"date": "2026-10-21", "open": 9.8, "close": 10.0, "high": 10.2, "low": 9.7, "volume": 1000000, "amount": 9900000.0},
    {"date": "2026-10-22", "open": 10.0, "close": 10.3, "high": 10.4, "low": 9.9, "volume": 1200000, "amount": 12200000.0},
    {"date": "2026-10-23", "open": 10.3, "close": 10.5, "high": 10.6, "low": 10.2, "volume": 900000, "amount": 9400000.0},
]


def _quote(code: str) -> dict[str, Any]:
    return {
        "code": code,
        "name": f"示例{code}",
        "price": 10.5,
        "pe": 12.3,
        "pb": 1.1,
        "total_market_cap": 2500.0,
        "float_market_cap": 2000.0,
        "timestamp": "20261023150003",
        "is_stale": False,
    }


def _fake_call(name: str, params: dict[str, Any] | None = None) -> Any:
    params = params or {}
    code = str(params.get("code") or "")
    if name == "eastmoney_financial_report":
        statement = str(params.get("statement") or "")
        return {"ok": True, "data": {"code": code, "statement": statement, "reports": FINANCIALS[statement]}}
    if name == "tencent_kline":
        return {
            "ok": True,
            "data": {"code": code, "period": "day", "adjust": "qfq", "klines": KLINES},
        }
    if name == "tencent_quote_batch":
        codes = [c for c in str(params.get("codes") or "").split(",") if c]
        quotes = {c: _quote(c) for c in codes if c != "000002"}
        failures = [{"code": c, "error": "no quote"} for c in codes if c == "000002"]
        return {
            "ok": True,
            "data": {"total": len(codes), "succeeded": len(quotes), "failed": len(failures), "quotes": quotes, "failures": failures},
        }
    raise AssertionError(f"unexpected tool call: {name}")


class _Stream:
    """Redis Stream double: records every XADD payload in order."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def ping(self) -> bool:
        return True

    def xadd(self, key: str, fields: dict[str, str], maxlen: int | None = None, approximate: bool = False) -> str:
        assert key == event_publisher.STREAM_KEY
        self.events.append(json.loads(fields["payload"]))
        return f"{len(self.events)}-0"


@pytest.fixture
def stream(monkeypatch: pytest.MonkeyPatch) -> _Stream:
    monkeypatch.setenv("EVENT_PUBLISH_ENABLE", "true")
    from tickhub_cn.core.config import reload_settings

    reload_settings()
    event_publisher.reset_publisher()
    fake = _Stream()

    import redis

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url, **kwargs: fake))
    return fake


def _prepare_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn import datasources
    from tickhub_cn.crawler import jobs

    monkeypatch.setattr(datasources, "call_tool", _fake_call)
    monkeypatch.setattr(jobs, "last_trading_day", lambda: "2026-10-23")
    monkeypatch.setattr(jobs, "now_shanghai", lambda: _dt.datetime(2026, 10, 26, 15, 30, tzinfo=_CN_TZ))


def _by_type(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(str(event["data_type"]), []).append(event)
    return grouped


def test_market_tasks_publish_each_new_row_once(monkeypatch: pytest.MonkeyPatch, stream: _Stream) -> None:
    _prepare_crawl(monkeypatch)

    financials = run_task("financials", codes="600000,000001")
    kline = run_task("kline", codes="600000,000001")
    quote = run_task("quote", codes="600000,000001")
    assert [financials["status"], kline["status"], quote["status"]] == ["ok", "ok", "ok"]
    # 2 codes × 3 statements × 2 report dates.
    assert financials["inserted"] == 12
    assert kline["inserted"] == 6
    assert quote["inserted"] == 2

    grouped = _by_type(stream.events)
    assert {kind: len(rows) for kind, rows in grouped.items()} == {
        "financial": 12,
        "kline": 6,
        "quote": 2,
        "heartbeat": 3,
    }
    ids = [event["event_id"] for event in stream.events]
    assert len(ids) == len(set(ids))
    assert {event["market"] for event in stream.events} == {"CN"}

    financial = {(e["symbol"], e["biz_key"]): e for e in grouped["financial"]}
    assert ("600000", "2026-09-30|balance") in financial
    assert ("000001", "2026-09-30|income") in financial
    assert ("600000", "2026-06-30|cashflow") in financial
    sample = financial[("600000", "2026-09-30|balance")]
    assert sample["symbol"] == "600000"
    assert sample["source_info"]["provider"] == "eastmoney"
    assert sample["event_time"] == "2026-09-30T00:00:00Z"
    fields = sample["content"]["structured_fields"]
    assert fields["report_type"] == "Q3"
    assert fields["statement"] == "balance"
    assert sample["content"]["quality_metrics"]["balance_check_result"]["status"] == "pass"
    assert sample["content"]["quality_metrics"]["quality_status"] == "normal"

    income = financial[("600000", "2026-09-30|income")]["content"]["structured_fields"]
    assert income["amount_revenue"] == "500.0000"
    assert income["amount_net_profit"] == "100.0000"

    bars = {e["biz_key"]: e for e in grouped["kline"]}
    assert set(bars) == {
        f"{day}|day|qfq" for day in ("2026-10-21", "2026-10-22", "2026-10-23")
    }
    bar = bars["2026-10-23|day|qfq"]["content"]["structured_fields"]
    assert bar["adjust"] == "qfq"
    assert bar["close"] == "10.5000"
    assert bar["volume"] == "900000"

    snapshots = {e["biz_key"]: e for e in grouped["quote"]}
    assert set(snapshots) == {"2026-10-23|150003"}
    snap = snapshots["2026-10-23|150003"]["content"]["structured_fields"]
    assert snap == {
        "trade_date": "2026-10-23",
        "close": "10.5000",
        "total_market_cap": "2500.0000",
        "pe_ttm": "12.3000",
        "pb": "1.1000",
    }

    archive = get_archive()
    assert archive.stats()["financial_report"] == 12
    assert archive.stats()["kline"] == 6
    assert archive.stats()["quote_snapshot"] == 2
    assert len(archive.financial_reports(code="600000")) == 6

    # A same-day rerun inserts nothing and adds nothing to the stream.
    streamed = len(stream.events)
    for task in ("financials", "kline", "quote"):
        again = run_task(task, codes="600000,000001")
        assert again["status"] == "ok"
        assert again["inserted"] == 0
    assert len(stream.events) == streamed


def test_quote_records_partial_failures(monkeypatch: pytest.MonkeyPatch, stream: _Stream) -> None:
    _prepare_crawl(monkeypatch)

    result = run_task("quote", codes="600000,000002")
    assert result["status"] == "partial"
    assert result["inserted"] == 1
    assert result["failed"] == ["000002"]
    assert get_archive().stats()["quote_snapshot"] == 1
    assert [e["data_type"] for e in stream.events].count("quote") == 1


def test_market_round_survives_redis_outage(monkeypatch: pytest.MonkeyPatch, stream: _Stream) -> None:
    _prepare_crawl(monkeypatch)
    import redis

    def down(url: str, **kwargs: Any) -> Any:
        raise redis.exceptions.ConnectionError("redis is down")

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(down))

    result = run_task("quote", codes="600000,000001")
    assert result["status"] == "ok"
    assert result["inserted"] == 2
    assert get_archive().stats()["quote_snapshot"] == 2
    assert stream.events == []
    # Two quotes + the round heartbeat are parked, not lost.
    assert event_publisher.stats()["outbox_pending"] == 3


def test_market_watchlist_never_falls_back_to_the_index(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.core.config import reload_settings
    from tickhub_cn.crawler.jobs import resolve_market_watchlist

    monkeypatch.setenv("OFFLINE_CRAWLER_WATCHLIST", "")
    reload_settings()
    assert resolve_market_watchlist() == []

    called: list[str] = []

    def boom(name: str, params: dict[str, Any] | None = None) -> Any:
        called.append(name)
        raise AssertionError(f"market tasks must not call {name} with an empty watchlist")

    from tickhub_cn import datasources

    monkeypatch.setattr(datasources, "call_tool", boom)
    result = run_task("kline")
    assert result["status"] == "empty"
    assert result["codes"] == 0
    assert result["inserted"] == 0
    stored = json.loads(get_archive().last_task("kline")["detail"])
    assert stored["note"] == "watchlist empty; set OFFLINE_CRAWLER_WATCHLIST"
    assert called == []
