"""CL-08: offline crawl writes are idempotent (docs/TEST_PLAN.md section 2)."""

from __future__ import annotations

from typing import Any

import pytest

from tickhub_cn.crawler.jobs import archive_announcements, run_task
from tickhub_cn.crawler.store import get_archive

pytestmark = pytest.mark.slow


ANNOUNCEMENTS = {
    "600000": [
        {"code": "600000", "title": "关于回购公司股份的公告", "publish_time": "2026-10-20", "id": "A1"},
        {"code": "600000", "title": "2026年第三季度报告", "publish_time": "2026-10-21", "id": "A2"},
    ],
    "000001": [
        {"code": "000001", "title": "关于股东减持计划的公告", "publish_time": "2026-10-22", "id": "A3"},
    ],
}

REPORTS = {
    "600000": [{"code": "600000", "title": "回购点评", "publish_date": "2026-10-21", "org": "示例证券", "id": "R1"}],
    "000001": [{"code": "000001", "title": "三季报点评", "publish_date": "2026-10-22", "org": "示例证券", "id": "R2"}],
}

NORTHBOUND = [
    {"channel": "hk2sh", "name": "沪股通", "net_inflow": 123456789.0, "net_inflow_wan": 123456.0, "status": 1},
    {"channel": "hk2sz", "name": "深股通", "net_inflow": -9876543.0, "net_inflow_wan": -9876.0, "status": 1},
    {"channel": "sh2hk", "name": "港股通(沪)", "net_inflow": 5550000.0, "net_inflow_wan": 555.0, "status": 1},
    {"channel": "sz2hk", "name": "港股通(深)", "net_inflow": 0.0, "net_inflow_wan": 0.0, "status": 1},
]

# The daily series shares (trade_date, channel) with the snapshot: 2026-10-23
# is last_trading_day() on the frozen clock, so those two rows enrich the
# snapshot above instead of adding new ones.
NORTHBOUND_HISTORY = [
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


def _fake_call(name: str, params: dict[str, Any] | None = None) -> Any:
    params = params or {}
    code = str(params.get("code") or "")
    if name == "cninfo_announcements":
        if code == "000001":
            raise RuntimeError("cninfo unavailable for this code")
        return {"ok": True, "data": {"announcements": ANNOUNCEMENTS.get(code, [])}}
    if name == "szse_announcements":
        return {"ok": True, "data": {"announcements": ANNOUNCEMENTS.get(code, [])}}
    if name == "eastmoney_reports":
        return {"ok": True, "data": {"reports": REPORTS.get(code, [])}}
    if name == "northbound_flow":
        if params.get("history"):
            return {"ok": True, "data": {"days": NORTHBOUND_HISTORY}, "meta": {"count": len(NORTHBOUND_HISTORY)}}
        return {"ok": True, "data": {"channels": NORTHBOUND}, "meta": {"count": len(NORTHBOUND)}}
    raise AssertionError(f"unexpected tool call: {name}")


def test_crawl_all_is_idempotent_and_archive_first(monkeypatch: pytest.MonkeyPatch) -> None:
    import tickhub_cn.datasources as datasources
    from tickhub_cn.crawler import jobs

    monkeypatch.setattr(datasources, "call_tool", _fake_call)
    monkeypatch.setattr(jobs, "last_trading_day", lambda: "2026-10-23")
    archive = get_archive()

    first = run_task("all", codes="600000,000001", days=3)
    assert first["status"] == "ok"
    assert first["codes"] == 2
    assert first["task_id"] > 0
    assert [t["task"] for t in first["tasks"]] == ["announcements", "reports", "risks", "northbound"]
    by_task = {t["task"]: t for t in first["tasks"]}
    assert by_task["announcements"]["fetched"] == 3
    assert by_task["announcements"]["failed"] == []
    assert by_task["reports"]["fetched"] == 2
    # The risk pass reads the archive, so it sees all three stored announcements.
    assert by_task["risks"]["fetched"] == 3
    # Northbound is not per-code: one snapshot covers all four channels, and
    # the daily backfill adds two turnover rows for the prior session.
    assert by_task["northbound"]["fetched"] == 8
    assert by_task["northbound"]["inserted"] == 6
    assert by_task["northbound"]["detail"]["history"] == {"ok": True, "rows": 4, "days": 2, "inserted": 2}
    assert first["fetched"] == 16
    assert first["inserted"] >= 5

    stats = archive.stats()
    assert stats["announcement"] == 3
    assert stats["research_report"] == 2
    # "回购" and "减持" both classify into risk events; the third title does not.
    assert stats["risk_event"] == 2
    assert stats["northbound_flow"] == 6

    rows = archive.announcements(code="600000", limit=10)
    assert {r["title"] for r in rows} == {"关于回购公司股份的公告", "2026年第三季度报告"}
    sources = {r["code"]: r["source"] for r in archive.announcements(limit=10)}
    assert sources == {"600000": "cninfo", "000001": "szse"}

    # The snapshot and the backfill enrich one row instead of duplicating it.
    today = {r["channel"]: r for r in archive.northbound(trade_date="2026-10-23")}
    assert today["hk2sh"]["net_inflow"] == 123456789.0
    assert today["hk2sh"]["deal_amount_wan"] == 15625100.0
    assert set(today["hk2sh"]["source"].split(",")) == {"eastmoney", "eastmoney_datacenter"}
    prior = archive.northbound(trade_date="2026-10-22")
    assert len(prior) == 2
    # Net buy is no longer published upstream; turnover still is.
    assert all(r["net_inflow"] is None for r in prior)
    assert all(r["deal_amount_wan"] for r in prior)

    second = run_task("all", codes="600000,000001", days=3)
    assert second["status"] == "ok"
    assert second["inserted"] == 0
    assert second["fetched"] == first["fetched"]
    for task in second["tasks"]:
        if task["task"] in ("risks", "northbound"):
            continue
        assert task["skipped"] == task["fetched"]
    # Northbound dedupes by (trade_date, channel) through an upsert, so the
    # second run refreshes the snapshot and the backfill without growing the table.
    second_nb = {t["task"]: t for t in second["tasks"]}["northbound"]
    assert second_nb["fetched"] == 8
    assert second_nb["inserted"] == 0

    again = archive.stats()
    assert again["announcement"] == stats["announcement"]
    assert again["research_report"] == stats["research_report"]
    assert again["risk_event"] == stats["risk_event"]
    assert again["northbound_flow"] == 6
    assert again["crawl_task"] == stats["crawl_task"] + 1

    last = archive.last_task()
    assert last["status"] == "ok"
    assert last["task"] == "all"

    # Archive-first reads never touch the network when rows already exist.
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("archive read must not hit the network")

    monkeypatch.setattr(datasources, "call_tool", forbidden)
    cached = archive_announcements("600000", limit=5)
    assert cached
    assert {r["title"] for r in cached} == {r["title"] for r in rows}


def test_opening_an_old_archive_adds_the_turnover_columns(tmp_path) -> None:
    import sqlite3

    from tickhub_cn.crawler.store import Archive

    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE northbound_flow ("
        " item_id TEXT PRIMARY KEY, trade_date TEXT, channel TEXT, name TEXT,"
        " net_inflow REAL, net_inflow_wan REAL, status INTEGER, source TEXT,"
        " snapshot_at TEXT, payload TEXT);"
        "INSERT INTO northbound_flow (item_id, trade_date, channel, name, net_inflow)"
        " VALUES ('eastmoney:2026-10-23:hk2sh', '2026-10-23', 'hk2sh', '沪股通', 1000.0);"
    )
    con.commit()
    con.close()

    archive = Archive(path)
    archive.insert_northbound(
        [
            {
                "trade_date": "2026-10-23",
                "channel": "hk2sh",
                "deal_amount_wan": 15625100.0,
                "deal_num": 8382898,
                "quota_text": "额度充足",
            }
        ],
        trade_date="2026-10-23",
        source="eastmoney_datacenter",
    )
    row = archive.northbound(trade_date="2026-10-23")[0]
    assert row["net_inflow"] == 1000.0
    assert row["deal_amount_wan"] == 15625100.0
    assert row["deal_num"] == 8382898
    assert row["quota_text"] == "额度充足"
