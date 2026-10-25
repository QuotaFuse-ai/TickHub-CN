"""CL-08: the northbound snapshot is archived once per trading day."""

from __future__ import annotations

from typing import Any

import pytest

from tickhub_cn.crawler import jobs
from tickhub_cn.crawler.store import get_archive

pytestmark = pytest.mark.slow


def _channel(net: float) -> list[dict[str, Any]]:
    return [
        {"channel": "hk2sh", "name": "沪股通", "net_inflow": net, "net_inflow_wan": net / 10, "status": 1},
        {"channel": "hk2sz", "name": "深股通", "net_inflow": -net / 2, "net_inflow_wan": -net / 20, "status": 1},
        {"channel": "sh2hk", "name": "港股通(沪)", "net_inflow": net / 4, "net_inflow_wan": net / 40, "status": 1},
        {"channel": "sz2hk", "name": "港股通(深)", "net_inflow": 0.0, "net_inflow_wan": 0.0, "status": 1},
    ]


def _open(net: float) -> dict[str, Any]:
    rows = _channel(net)
    return {"ok": True, "data": {"channels": rows}, "meta": {"count": len(rows)}}


def _closed() -> dict[str, Any]:
    return {
        "ok": True,
        "data": [],
        "meta": {"note": "northbound closed or not yet published", "closed": True, "channels": _channel(0.0)},
    }


# The daily datacenter series: turnover for two sessions, no net buy (the
# exchange stopped publishing it after 2024-08-16).
HISTORY = [
    {
        "trade_date": "2026-10-23",
        "channel": "hk2sh",
        "name": "沪股通",
        "deal_amount_wan": 15625100.0,
        "deal_num": 8382898,
        "net_inflow": None,
        "quota_text": "额度充足",
    },
    {
        "trade_date": "2026-10-23",
        "channel": "hk2sz",
        "name": "深股通",
        "deal_amount_wan": 15692726.0,
        "deal_num": 8731057,
        "net_inflow": None,
        "quota_text": "额度充足",
    },
    {
        "trade_date": "2026-10-22",
        "channel": "hk2sh",
        "name": "沪股通",
        "deal_amount_wan": 14900000.0,
        "deal_num": 8000000,
        "net_inflow": None,
        "quota_text": "额度充足",
    },
    {
        "trade_date": "2026-10-22",
        "channel": "hk2sz",
        "name": "深股通",
        "deal_amount_wan": 14800000.0,
        "deal_num": 7900000,
        "net_inflow": None,
        "quota_text": "额度充足",
    },
]


def _history() -> dict[str, Any]:
    return {"ok": True, "data": {"days": HISTORY}, "meta": {"count": len(HISTORY)}}


def test_snapshot_is_written_once_and_refreshed_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    import tickhub_cn.datasources as datasources

    monkeypatch.setattr(jobs, "last_trading_day", lambda: "2026-10-23")
    payloads = [_open(1000.0), _open(2000.0)]

    def fake_call(name: str, params: dict[str, Any] | None = None) -> Any:
        if name != "northbound_flow":
            raise AssertionError(f"unexpected tool call: {name}")
        if (params or {}).get("history"):
            return {"ok": True, "data": {"days": []}, "meta": {"count": 0}}
        return payloads.pop(0)

    monkeypatch.setattr(datasources, "call_tool", fake_call)
    archive = get_archive()

    first = jobs.run_task("northbound", codes="600000")
    assert first["status"] == "ok"
    assert first["task_id"] > 0
    assert first["fetched"] == 4
    assert first["inserted"] == 4
    assert [r["trade_date"] for r in archive.northbound()] == ["2026-10-23"] * 4

    # A later sample on the same day replaces the row instead of adding one.
    second = jobs.run_task("northbound", codes="600000")
    assert second["status"] == "ok"
    assert second["fetched"] == 4
    assert second["inserted"] == 0

    rows = {r["channel"]: r for r in archive.northbound(trade_date="2026-10-23")}
    assert len(rows) == 4
    assert rows["hk2sh"]["net_inflow"] == 2000.0
    assert rows["hk2sz"]["net_inflow"] == -1000.0
    assert rows["hk2sh"]["status"] == 1
    assert rows["hk2sh"]["snapshot_at"]


def test_a_closed_session_writes_no_snapshot_but_still_backfills(monkeypatch: pytest.MonkeyPatch) -> None:
    import tickhub_cn.datasources as datasources

    monkeypatch.setattr(jobs, "last_trading_day", lambda: "2026-10-23")
    archive = get_archive()

    def opening(name: str, params: dict[str, Any] | None = None) -> Any:
        if (params or {}).get("history"):
            return {"ok": True, "data": {"days": []}, "meta": {"count": 0}}
        return _open(1000.0)

    monkeypatch.setattr(datasources, "call_tool", opening)
    jobs.run_task("northbound", codes="600000")
    before = archive.northbound(trade_date="2026-10-23")
    assert len(before) == 4
    assert all(r["deal_amount_wan"] is None for r in before)

    def closed_call(name: str, params: dict[str, Any] | None = None) -> Any:
        if name != "northbound_flow":
            raise AssertionError(f"unexpected tool call: {name}")
        if (params or {}).get("history"):
            return _history()
        return _closed()

    monkeypatch.setattr(datasources, "call_tool", closed_call)
    result = jobs.run_task("northbound", codes="600000")
    assert result["status"] == "ok"
    # A closed session adds no snapshot, but the daily backfill still lands.
    assert result["fetched"] == 4
    assert result["inserted"] == 2

    after = archive.northbound(trade_date="2026-10-23")
    assert [r["net_inflow"] for r in after] == [r["net_inflow"] for r in before]
    turnover = {r["channel"]: r["deal_amount_wan"] for r in after}
    assert turnover["hk2sh"] == 15625100.0
    assert turnover["hk2sz"] == 15692726.0
    assert turnover["sh2hk"] is None


def test_history_backfill_fills_the_table_and_keeps_the_snapshot_net(monkeypatch: pytest.MonkeyPatch) -> None:
    import tickhub_cn.datasources as datasources

    monkeypatch.setattr(jobs, "last_trading_day", lambda: "2026-10-23")

    def fake_call(name: str, params: dict[str, Any] | None = None) -> Any:
        if name != "northbound_flow":
            raise AssertionError(f"unexpected tool call: {name}")
        if (params or {}).get("history"):
            return _history()
        return _open(1000.0)

    monkeypatch.setattr(datasources, "call_tool", fake_call)
    archive = get_archive()

    result = jobs.run_task("northbound", codes="600000")
    assert result["status"] == "ok"
    # 4 snapshot rows + 2 new rows for the prior session; the other two history
    # rows share their key with the snapshot.
    assert result["fetched"] == 8
    assert result["inserted"] == 6
    assert result["tasks"][0]["detail"]["history"] == {"ok": True, "rows": 4, "days": 2, "inserted": 2}

    rows = {r["channel"]: r for r in archive.northbound(trade_date="2026-10-23")}
    assert rows["hk2sh"]["net_inflow"] == 1000.0
    assert rows["hk2sh"]["deal_amount_wan"] == 15625100.0
    assert rows["hk2sh"]["deal_num"] == 8382898
    assert rows["hk2sh"]["quota_text"] == "额度充足"
    assert set(rows["hk2sh"]["source"].split(",")) == {"eastmoney", "eastmoney_datacenter"}

    prior = {r["channel"]: r for r in archive.northbound(trade_date="2026-10-22")}
    assert prior["hk2sh"]["net_inflow"] is None
    assert prior["hk2sh"]["deal_amount_wan"] == 14900000.0

    again = jobs.run_task("northbound", codes="600000")
    assert again["inserted"] == 0
    assert archive.stats()["northbound_flow"] == 6


def test_a_failed_snapshot_is_reported_without_touching_the_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    import tickhub_cn.datasources as datasources

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("kamt unavailable")

    monkeypatch.setattr(jobs, "last_trading_day", lambda: "2026-10-23")
    monkeypatch.setattr(datasources, "call_tool", broken)
    archive = get_archive()

    result = jobs.run_task("northbound", codes="600000")
    assert result["status"] == "failed"
    assert result["failed"] == ["northbound"]
    assert result["inserted"] == 0
    assert archive.stats()["northbound_flow"] == 0
