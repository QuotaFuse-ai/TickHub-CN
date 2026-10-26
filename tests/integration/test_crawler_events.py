"""T-101: a crawl publishes each newly archived row once, even when Redis blips."""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

import pytest

from tickhub_cn.crawler import event_publisher
from tickhub_cn.crawler.jobs import run_task

pytestmark = pytest.mark.slow

_CN_TZ = _dt.timezone(_dt.timedelta(hours=8))


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
    "600000": [
        {
            "code": "600000",
            "title": "回购点评",
            "publish_date": "2026-10-21",
            "org": "示例证券",
            "info_code": "R1",
        }
    ],
    "000001": [
        {
            "code": "000001",
            "title": "三季报点评",
            "publish_date": "2026-10-22",
            "org": "示例证券",
            "info_code": "R2",
        }
    ],
}

NORTHBOUND = [
    {"channel": "hk2sh", "name": "沪股通", "net_inflow": 123456789.0, "net_inflow_wan": 123456.0, "status": 1},
    {"channel": "hk2sz", "name": "深股通", "net_inflow": -9876543.0, "net_inflow_wan": -9876.0, "status": 1},
    {"channel": "sh2hk", "name": "港股通(沪)", "net_inflow": 5550000.0, "net_inflow_wan": 555.0, "status": 1},
    {"channel": "sz2hk", "name": "港股通(深)", "net_inflow": 0.0, "net_inflow_wan": 0.0, "status": 1},
]

# 2026-10-23 rows enrich the snapshot above; the 2026-10-22 rows are new.
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
    # A frozen round clock keeps the heartbeat run_id (and so the rerun) stable.
    monkeypatch.setattr(jobs, "now_shanghai", lambda: _dt.datetime(2026, 10, 26, 15, 30, tzinfo=_CN_TZ))


def _by_type(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(str(event["data_type"]), []).append(event)
    return grouped


def test_crawl_publishes_each_new_row_once(monkeypatch: pytest.MonkeyPatch, stream: _Stream) -> None:
    _prepare_crawl(monkeypatch)

    first = run_task("all", codes="600000,000001", days=3)
    assert first["status"] == "ok"

    grouped = _by_type(stream.events)
    assert {kind: len(rows) for kind, rows in grouped.items()} == {
        "announcement": 3,
        "research": 2,
        "risk": 2,
        "northbound": 6,
        "heartbeat": 1,
    }
    ids = [event["event_id"] for event in stream.events]
    assert len(ids) == len(set(ids))

    announcements = {e["content"]["structured_fields"]["title"]: e for e in grouped["announcement"]}
    assert announcements["关于回购公司股份的公告"]["source_info"]["provider"] == "cninfo"
    assert announcements["关于股东减持计划的公告"]["source_info"]["provider"] == "szse"
    assert announcements["关于回购公司股份的公告"]["event_time"] == "2026-10-20T00:00:00Z"
    # A failed source is recorded but does not stop the round's events.
    assert announcements["关于股东减持计划的公告"]["symbol"] == "000001"

    reports = {e["biz_key"] for e in grouped["research"]}
    assert reports == {"R1", "R2"}

    northbound = {e["biz_key"]: e for e in grouped["northbound"]}
    assert set(northbound) == {
        "2026-10-23|hk2sh",
        "2026-10-23|hk2sz",
        "2026-10-23|sh2hk",
        "2026-10-23|sz2hk",
        "2026-10-22|hk2sh",
        "2026-10-22|hk2sz",
    }
    snapshot = northbound["2026-10-23|hk2sh"]["content"]["structured_fields"]
    assert snapshot["net_buy_amount"] == "123456789.0000"
    # The row was new when the snapshot landed; the history pass enriched the
    # archive but did not republish it, so the turnover rides on the prior day.
    prior = northbound["2026-10-22|hk2sh"]["content"]["structured_fields"]
    assert prior["deal_amount_wan"] == "14900000.0000"
    assert prior["deal_num"] == 8000000

    heartbeat = grouped["heartbeat"][0]
    assert heartbeat["biz_key"] == "cn-20261026-all"
    assert heartbeat["content"]["structured_fields"]["status"] == "ok"
    assert heartbeat["content"]["structured_fields"]["items_collected"] == first["inserted"]
    assert heartbeat["event_time"] == "2026-10-26T07:30:00Z"

    # A rerun of the same window finds every row present and adds nothing,
    # neither data events (only new rows publish) nor a second heartbeat.
    streamed = len(stream.events)
    second = run_task("all", codes="600000,000001", days=3)
    assert second["status"] == "ok"
    assert second["inserted"] == 0
    assert len(stream.events) == streamed


def test_redis_outage_parks_events_and_flushes_after_recovery(
    monkeypatch: pytest.MonkeyPatch, stream: _Stream
) -> None:
    _prepare_crawl(monkeypatch)
    import redis

    from tickhub_cn.crawler.store import get_archive

    def down(url: str, **kwargs: Any) -> Any:
        raise redis.exceptions.ConnectionError("redis is down")

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(down))

    result = run_task("announcements", codes="600000", days=3)
    assert result["status"] == "ok"
    assert result["inserted"] == 2
    assert get_archive().stats()["announcement"] == 2

    # The crawl succeeded; its four events (two announcements, one computed
    # risk, one heartbeat) are parked instead of lost.
    parked = event_publisher.stats()
    assert parked["outbox_pending"] == 4
    assert parked["publish_failed_total"] == 4
    assert stream.events == []

    publisher = event_publisher.get_publisher()
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url, **kwargs: stream))
    publisher._connect_backoff_until = 0.0
    with publisher._db() as con:
        con.execute("UPDATE event_outbox SET next_retry_at=?", ("2000-01-01T00:00:00Z",))

    assert event_publisher.flush_outbox() == 4
    assert len(stream.events) == 4
    assert {event["data_type"] for event in stream.events} == {"announcement", "risk", "heartbeat"}
    assert event_publisher.stats()["outbox_pending"] == 0
    # Re-delivery is once only: the outbox is empty and the stream is stable.
    assert event_publisher.flush_outbox() == 0
    assert len(stream.events) == 4


def test_idle_tick_flushes_the_outbox_and_reports_liveness(
    monkeypatch: pytest.MonkeyPatch, stream: _Stream
) -> None:
    import redis

    from tickhub_cn.crawler import daemon

    parked = event_publisher.build_event(
        provider="cninfo",
        data_type="announcement",
        symbol="600000",
        market="CN",
        biz_key="cninfo:A1",
        event_time="2026-10-20",
        content={"structured_fields": {"title": "关于回购公司股份的公告"}},
    )
    monkeypatch.setattr(
        redis.Redis,
        "from_url",
        staticmethod(lambda url, **kwargs: (_ for _ in ()).throw(redis.exceptions.ConnectionError("down"))),
    )
    event_publisher.publish(parked)
    assert event_publisher.stats()["outbox_pending"] == 1

    publisher = event_publisher.get_publisher()
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url, **kwargs: stream))
    publisher._connect_backoff_until = 0.0
    with publisher._db() as con:
        con.execute("UPDATE event_outbox SET next_retry_at=?", ("2000-01-01T00:00:00Z",))

    monkeypatch.setattr(daemon, "now_shanghai", lambda: _dt.datetime(2026, 10, 26, 15, 35, tzinfo=_CN_TZ))
    daemon._event_tick()

    assert [event["data_type"] for event in stream.events] == ["announcement", "heartbeat"]
    assert stream.events[0]["event_id"] == parked["event_id"]
    idle = stream.events[1]
    # The idle tick must not collide with the daily round's run_id.
    assert idle["biz_key"] == "cn-20261026-idle-1535"
    assert idle["content"]["structured_fields"] == {
        "run_id": "cn-20261026-idle-1535",
        "status": "ok",
        "items_collected": 0,
        "finished_at": "2026-10-26T07:35:00Z",
    }
    assert event_publisher.stats()["outbox_pending"] == 0
