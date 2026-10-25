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
        return {"ok": True, "data": {"channels": NORTHBOUND}, "meta": {"count": len(NORTHBOUND)}}
    raise AssertionError(f"unexpected tool call: {name}")


def test_crawl_all_is_idempotent_and_archive_first(monkeypatch: pytest.MonkeyPatch) -> None:
    import tickhub_cn.datasources as datasources

    monkeypatch.setattr(datasources, "call_tool", _fake_call)
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
    # Northbound is not per-code: one snapshot covers all four channels.
    assert by_task["northbound"]["fetched"] == 4
    assert by_task["northbound"]["inserted"] == 4
    assert first["fetched"] == 12
    assert first["inserted"] >= 5

    stats = archive.stats()
    assert stats["announcement"] == 3
    assert stats["research_report"] == 2
    # "回购" and "减持" both classify into risk events; the third title does not.
    assert stats["risk_event"] == 2
    assert stats["northbound_flow"] == 4

    rows = archive.announcements(code="600000", limit=10)
    assert {r["title"] for r in rows} == {"关于回购公司股份的公告", "2026年第三季度报告"}
    sources = {r["code"]: r["source"] for r in archive.announcements(limit=10)}
    assert sources == {"600000": "cninfo", "000001": "szse"}

    second = run_task("all", codes="600000,000001", days=3)
    assert second["status"] == "ok"
    assert second["inserted"] == 0
    assert second["fetched"] == first["fetched"]
    for task in second["tasks"]:
        if task["task"] in ("risks", "northbound"):
            continue
        assert task["skipped"] == task["fetched"]
    # Northbound dedupes by (trade_date, channel) through an upsert, so the
    # second run refreshes the snapshot without growing the table.
    second_nb = {t["task"]: t for t in second["tasks"]}["northbound"]
    assert second_nb["fetched"] == 4
    assert second_nb["inserted"] == 0

    again = archive.stats()
    assert again["announcement"] == stats["announcement"]
    assert again["research_report"] == stats["research_report"]
    assert again["risk_event"] == stats["risk_event"]
    assert again["northbound_flow"] == 4
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
