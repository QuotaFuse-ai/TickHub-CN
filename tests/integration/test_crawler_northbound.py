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


def test_snapshot_is_written_once_and_refreshed_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    import tickhub_cn.datasources as datasources

    monkeypatch.setattr(jobs, "last_trading_day", lambda: "2026-10-23")
    payloads = [_open(1000.0), _open(2000.0)]

    def fake_call(name: str, _params: dict[str, Any] | None = None) -> Any:
        if name != "northbound_flow":
            raise AssertionError(f"unexpected tool call: {name}")
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


def test_a_closed_session_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import tickhub_cn.datasources as datasources

    monkeypatch.setattr(jobs, "last_trading_day", lambda: "2026-10-23")
    archive = get_archive()

    monkeypatch.setattr(datasources, "call_tool", lambda *_a, **_k: _open(1000.0))
    jobs.run_task("northbound", codes="600000")
    before = archive.northbound()
    assert len(before) == 4

    monkeypatch.setattr(datasources, "call_tool", lambda *_a, **_k: _closed())
    result = jobs.run_task("northbound", codes="600000")
    assert result["status"] == "ok"
    assert result["fetched"] == 0
    assert result["inserted"] == 0

    after = archive.northbound()
    assert [r["net_inflow"] for r in after] == [r["net_inflow"] for r in before]


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
