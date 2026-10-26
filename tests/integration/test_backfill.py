"""T-104: the one-off backfill replays archived rows exactly once."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tickhub_cn.core.config import get_settings, reload_settings
from tickhub_cn.crawler import event_publisher, store

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.slow


def _load_script() -> Any:
    path = ROOT / "scripts" / "backfill_cn.py"
    spec = importlib.util.spec_from_file_location("backfill_cn", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backfill = _load_script()


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


def _outbox() -> Path:
    return Path(get_settings().archive_dir) / "event_outbox.db"


def _pending() -> int:
    if not _outbox().exists():
        return 0
    con = sqlite3.connect(_outbox())
    try:
        try:
            return int(con.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0])
        except sqlite3.OperationalError:  # ledger-only file, no outbox table yet
            return 0
    finally:
        con.close()


def _retry_now() -> None:
    con = sqlite3.connect(_outbox())
    try:
        con.execute("UPDATE event_outbox SET next_retry_at='2000-01-01T00:00:00Z'")
        con.commit()
    finally:
        con.close()


@pytest.fixture
def archive() -> Path:
    """Archive filled through the real crawl writes; publishing stays off."""
    arc = store.Archive()
    arc.insert_announcements(
        [
            {"code": "600000", "title": "回购公告", "publish_time": "2026-10-20 18:00:00", "id": "A1"},
            {"code": "000001", "title": "减持公告", "publish_time": "2026-10-21 19:30:00", "id": "A2"},
        ],
        source="cninfo",
    )
    arc.insert_reports(
        [
            {
                "code": "600000",
                "title": "回购点评",
                "publish_date": "2026-10-21 08:00:00",
                "org": "示例证券",
                "info_code": "R1",
            }
        ],
        source="eastmoney",
    )
    arc.insert_risks(
        [
            {
                "code": "600000",
                "type": "goodwill_impairment",
                "date": "2026-10-22",
                "title": "计提商誉减值",
                "severity": "high",
                "id": "K1",
            }
        ],
        source="computed",
    )
    arc.insert_northbound(
        [
            {
                "channel": "hk2sh",
                "name": "沪股通",
                "net_inflow": 123456789.0,
                "deal_amount_wan": 5000.0,
                "deal_num": 10,
                "quota_text": "充足",
            }
        ],
        trade_date="2026-10-23",
        source="eastmoney_datacenter",
        snapshot_at="2026-10-23 15:05:00",
    )
    return arc.path


@pytest.fixture
def stream(monkeypatch: pytest.MonkeyPatch) -> _Stream:
    """Publishing on, with a fake Redis Stream."""
    monkeypatch.setenv("EVENT_PUBLISH_ENABLE", "true")
    reload_settings()
    event_publisher.reset_publisher()
    fake = _Stream()

    import redis

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url, **kwargs: fake))
    return fake


def test_dry_run_counts_only(archive: Path, stream: _Stream, capsys: pytest.CaptureFixture[str]) -> None:
    assert backfill.main(["--db", str(archive), "--dry-run"]) == 0
    assert stream.events == []
    assert not _outbox().exists()
    output = capsys.readouterr().out
    assert "dry-run" in output
    assert "total" in output


def test_backfill_publishes_the_archive_once(archive: Path, stream: _Stream) -> None:
    assert backfill.main(["--db", str(archive)]) == 0
    assert len(stream.events) == 5
    assert all(event["source_info"]["backfill"] is True for event in stream.events)
    assert all(event["source_info"]["crawl_time"] for event in stream.events)
    for event in stream.events:
        assert event["event_id"] == event_publisher.event_id(
            event["source_info"]["provider"],
            event["data_type"],
            event["symbol"],
            event["biz_key"],
        )
    by_type = {event["data_type"]: event for event in stream.events}
    first_announcement = next(e for e in stream.events if e["biz_key"] == "cninfo:A1")
    assert first_announcement["event_time"] == "2026-10-20T10:00:00Z"
    assert by_type["northbound"]["symbol"] is None
    assert by_type["northbound"]["content"]["structured_fields"]["net_buy_amount"] == "123456789.0000"
    assert _pending() == 0


def test_rerun_sends_nothing(archive: Path, stream: _Stream) -> None:
    assert backfill.main(["--db", str(archive)]) == 0
    delivered = list(stream.events)
    assert backfill.main(["--db", str(archive)]) == 0
    assert stream.events == delivered


def test_outbox_parks_when_redis_is_down_and_recovery_sends_once(
    archive: Path, stream: _Stream, monkeypatch: pytest.MonkeyPatch
) -> None:
    import redis

    def down(url: str, **kwargs: Any) -> Any:
        raise redis.exceptions.ConnectionError("redis is down")

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(down))
    assert backfill.main(["--db", str(archive)]) == 0
    assert stream.events == []
    assert _pending() == 5

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url, **kwargs: stream))
    _retry_now()
    assert backfill.main(["--db", str(archive)]) == 0
    assert len(stream.events) == 5
    assert _pending() == 0

    assert backfill.main(["--db", str(archive)]) == 0
    assert len(stream.events) == 5


def test_direct_delivery_supersedes_a_parked_outbox_row(
    archive: Path, stream: _Stream, monkeypatch: pytest.MonkeyPatch
) -> None:
    import redis

    def down(url: str, **kwargs: Any) -> Any:
        raise redis.exceptions.ConnectionError("redis is down")

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(down))
    assert backfill.main(["--db", str(archive)]) == 0
    assert _pending() == 5

    # Redis is back but the parked rows are not due yet: the scan must deliver
    # them and retire the stale rows, not leave them to be flushed a second time.
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url, **kwargs: stream))
    con = sqlite3.connect(_outbox())
    try:
        con.execute("UPDATE event_outbox SET next_retry_at='2099-01-01T00:00:00Z'")
        con.commit()
    finally:
        con.close()

    assert backfill.main(["--db", str(archive)]) == 0
    assert len(stream.events) == 5
    assert _pending() == 0
    assert backfill.main(["--db", str(archive)]) == 0
    assert len(stream.events) == 5
