"""T-101: FinanceCoffer event envelope, ids and publisher behaviour."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import threading
import time
from typing import Any

import pytest

from tickhub_cn.crawler import event_publisher

pytestmark = pytest.mark.contract

_CN_TZ = _dt.timezone(_dt.timedelta(hours=8))


def _sha(provider: str, data_type: str, symbol: str | None, biz_key: str) -> str:
    raw = f"{provider}|{data_type}|{symbol or ''}|{biz_key}"
    return "evt_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class _Stream:
    """Redis double that records the XADD payloads in order."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def ping(self) -> bool:
        return True

    def xadd(
        self, key: str, fields: dict[str, str], maxlen: int | None = None, approximate: bool = False
    ) -> str:
        assert key == event_publisher.STREAM_KEY
        assert maxlen == event_publisher.MAXLEN
        assert approximate is True
        self.events.append(json.loads(fields["payload"]))
        return f"{len(self.events)}-0"


class _GatedStream(_Stream):
    """Records payloads only after the test opens the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def xadd(self, key: str, fields: dict[str, str], maxlen: int | None = None, approximate: bool = False) -> str:
        self.entered.set()
        assert self.release.wait(10)
        return super().xadd(key, fields, maxlen, approximate)


def _enable_publishing(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setenv("EVENT_PUBLISH_ENABLE", "true")
    from tickhub_cn.core.config import reload_settings

    reload_settings()
    event_publisher.reset_publisher()
    import redis

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url, **kwargs: fake))


@pytest.fixture
def stream(monkeypatch: pytest.MonkeyPatch) -> _Stream:
    """Publishing on, with a fake Redis Stream."""
    fake = _Stream()
    _enable_publishing(monkeypatch, fake)
    return fake


def _event(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "provider": "cninfo",
        "data_type": "announcement",
        "symbol": "600000",
        "market": "CN",
        "biz_key": "cninfo:A1",
        "event_time": "2026-10-20",
        "content": {"structured_fields": {}},
    }
    params.update(overrides)
    return event_publisher.build_event(**params)


def test_event_id_is_a_deterministic_sha256_hash() -> None:
    first = event_publisher.event_id("eastmoney", "research", "600000", "R1")
    assert first == _sha("eastmoney", "research", "600000", "R1")
    assert first == event_publisher.event_id("eastmoney", "research", "600000", "R1")
    assert len(first) == len("evt_") + 32
    # A missing symbol contributes an empty segment, not "None".
    assert event_publisher.event_id("tickhub-cn", "heartbeat", None, "run-1") == _sha(
        "tickhub-cn", "heartbeat", None, "run-1"
    )


def test_build_event_fills_the_v02_envelope() -> None:
    event = event_publisher.build_event(
        provider="eastmoney_datacenter",
        data_type="northbound",
        symbol=None,
        market="CN",
        biz_key="2026-10-23|hk2sh",
        event_time="2026-10-23",
        content={"structured_fields": {"trade_date": "2026-10-23"}},
        source_info={"provider": "eastmoney_datacenter"},
    )
    assert event["schema_version"] == "1.0"
    assert event["event_id"] == _sha("eastmoney_datacenter", "northbound", None, "2026-10-23|hk2sh")
    assert event["event_time"] == "2026-10-23T00:00:00Z"  # a business day keeps its calendar date
    assert event["symbol"] is None
    assert event["market"] == "CN"
    assert event["source_info"]["provider"] == "eastmoney_datacenter"
    assert event["source_info"]["crawl_time"].endswith("Z")
    assert event["content"]["quality_metrics"] == {"confidence": "high", "quality_status": "normal"}


def test_event_time_is_business_time() -> None:
    # The crawler pins local times to Asia/Shanghai before calling; the
    # envelope only normalises to UTC and leaves naive input on its calendar day.
    aware = _event(event_time=_dt.datetime(2026, 10, 23, 9, 30, tzinfo=_CN_TZ))
    assert aware["event_time"] == "2026-10-23T01:30:00Z"
    naive = _event(event_time="2026-10-23 09:30:00")
    assert naive["event_time"] == "2026-10-23T09:30:00Z"
    iso = _event(event_time="2026-10-23T09:30:00+08:00")
    assert iso["event_time"] == "2026-10-23T01:30:00Z"


def test_publish_xadds_the_envelope(stream: _Stream) -> None:
    event = _event()
    event_publisher.publish(event)
    assert event_publisher.wait_idle(5) is True
    assert stream.events == [event]
    stats = event_publisher.stats()
    assert stats["queued"] == 0
    assert stats["published_total"] == 1
    assert stats["publish_failed_total"] == 0
    assert stats["outbox_pending"] == 0


def test_disabled_publisher_writes_nothing() -> None:
    event_publisher.reset_publisher()
    assert event_publisher.is_enabled() is False
    event_publisher.publish(_event())
    event_publisher.heartbeat(provider="tickhub-cn", market="CN", run_id="cn-20261026-all", status="ok")
    assert event_publisher.flush_outbox() == 0
    stats = event_publisher.stats()
    assert stats["enabled"] is False
    assert stats["outbox_pending"] == 0
    assert not event_publisher.get_publisher().outbox_path.exists()


def test_heartbeat_is_one_event_per_run(stream: _Stream) -> None:
    started = _dt.datetime(2026, 10, 26, 2, 0, tzinfo=_CN_TZ)
    finished = started + _dt.timedelta(minutes=5)
    for _ in range(2):  # the same round republished is still one fact
        event_publisher.heartbeat(
            provider="tickhub-cn",
            market="CN",
            run_id="cn-20261026-all",
            status="partial",
            items=7,
            started_at=started,
            finished_at=finished,
        )
    # A manual re-run is a new process, so the claim must outlive the publisher.
    event_publisher.reset_publisher()
    event_publisher.heartbeat(
        provider="tickhub-cn", market="CN", run_id="cn-20261026-all", status="ok", finished_at=finished
    )
    # A different round is a different fact and still goes out.
    event_publisher.heartbeat(
        provider="tickhub-cn", market="CN", run_id="cn-20261026-reports", status="ok", finished_at=finished
    )
    assert event_publisher.wait_idle(5) is True
    assert len(stream.events) == 2
    assert [e["biz_key"] for e in stream.events] == ["cn-20261026-all", "cn-20261026-reports"]
    event = stream.events[0]
    assert event["data_type"] == "heartbeat"
    assert event["symbol"] is None
    assert event["biz_key"] == "cn-20261026-all"
    assert event["event_id"] == _sha("tickhub-cn", "heartbeat", None, "cn-20261026-all")
    assert event["event_time"] == "2026-10-25T18:05:00Z"
    assert event["content"]["structured_fields"] == {
        "run_id": "cn-20261026-all",
        "status": "partial",
        "items_collected": 7,
        "finished_at": "2026-10-25T18:05:00Z",
        "started_at": "2026-10-25T18:00:00Z",
    }


def test_outbox_parks_delivery_and_flush_sends_once(stream: _Stream, monkeypatch: pytest.MonkeyPatch) -> None:
    import redis

    publisher = event_publisher.get_publisher()

    def down(url: str, **kwargs: Any) -> Any:
        raise redis.exceptions.ConnectionError("redis is down")

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(down))
    event = _event()
    event_publisher.publish(event)
    assert event_publisher.wait_idle(5) is True
    assert stream.events == []
    stats = event_publisher.stats()
    assert stats["outbox_pending"] == 1
    assert stats["publish_failed_total"] == 1

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url, **kwargs: stream))
    publisher._connect_backoff_until = 0
    with publisher._db() as con:
        con.execute("UPDATE event_outbox SET next_retry_at=?", ("2000-01-01T00:00:00Z",))
    assert event_publisher.flush_outbox() == 1
    assert stream.events == [event]
    assert event_publisher.stats()["outbox_pending"] == 0

    assert event_publisher.flush_outbox() == 0
    assert stream.events == [event]


def test_publish_hands_off_to_the_background_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    slow = _GatedStream()
    _enable_publishing(monkeypatch, slow)
    started = time.monotonic()
    event_publisher.publish(_event())
    # The crawler must not wait for XADD: the worker is still blocked inside it.
    assert time.monotonic() - started < 1.0
    assert slow.entered.wait(5) is True
    assert slow.events == []

    slow.release.set()
    assert event_publisher.wait_idle(5) is True
    assert len(slow.events) == 1
    assert event_publisher.stats()["published_total"] == 1
    event_publisher.reset_publisher()


def test_full_queue_parks_events_in_the_outbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(event_publisher, "QUEUE_MAXSIZE", 1)
    slow = _GatedStream()
    import redis

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url, **kwargs: slow))
    publisher = event_publisher.EventPublisher(enabled=True, outbox_path=tmp_path / "outbox.db")
    try:
        first, second, third = (_event(biz_key=f"cninfo:A{index}") for index in (1, 2, 3))
        publisher.publish(first)  # the worker takes it and blocks in XADD
        assert slow.entered.wait(5) is True
        publisher.publish(second)  # fills the single queue slot
        started = time.monotonic()
        publisher.publish(third)  # queue full: parked immediately, no blocking
        assert time.monotonic() - started < 1.0
        assert publisher.stats()["outbox_pending"] == 1

        slow.release.set()
        assert publisher.wait_idle(5) is True
        assert [event["biz_key"] for event in slow.events] == ["cninfo:A1", "cninfo:A2"]
        assert publisher.stats()["published_total"] == 2
    finally:
        publisher.close()
