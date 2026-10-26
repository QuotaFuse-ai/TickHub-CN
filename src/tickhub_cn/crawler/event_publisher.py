"""FinanceCoffer event publishing side-channel (spec T-101).

The crawler keeps writing the local SQLite archive exactly as before; every
newly inserted row is also published as one event on the ``fc:stockdata:events``
Redis Stream. Publishing is a side path: a Redis outage must never fail a
crawl, so a failed delivery is parked in the local ``event_outbox`` table and
retried later with exponential backoff. ``event_id`` is a deterministic hash of
the business fact, which is what makes outbox retries and re-runs idempotent on
the consumer side.

Nothing here touches the network at import time; the Redis client is created on
first use and the module is inert while ``EVENT_PUBLISH_ENABLE`` is false.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..core.config import get_settings
from ..logging.setup import get_logger

STREAM_KEY = "fc:stockdata:events"
MAXLEN = 100_000
SCHEMA_VERSION = "1.0"

# 1s / 5s / 25s / 2m / 10m, then stay at the last step for further attempts.
BACKOFF_SECONDS = (1, 5, 25, 120, 600)
AGED_AFTER_SECONDS = 24 * 3600
AGED_ALERT_INTERVAL = 3600
CONNECT_BACKOFF_SECONDS = 30
PUBLISH_TIMEOUT_SECONDS = 2.0
IN_CHUNK = 500

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_outbox (
  event_id      TEXT PRIMARY KEY,
  payload       TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  attempts      INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT NOT NULL,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_outbox_retry ON event_outbox(next_retry_at);
"""


def _log() -> Any:
    return get_logger("crawler")


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(value: _dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return value.astimezone(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: Any) -> _dt.datetime:
    """Business time as UTC. Naive input is treated as UTC so date-only values
    keep their calendar day in ``event_time``."""
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=_dt.timezone.utc)
    text = str(value or "").strip()
    if not text:
        return _utc_now()
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                parsed = _dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return _utc_now()
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=_dt.timezone.utc)


def _redact(url: str) -> str:
    return re.sub(r"://([^/@]+)@", "://***@", url or "")


def event_id(provider: str, data_type: str, symbol: str | None, biz_key: str) -> str:
    raw = f"{provider}|{data_type}|{symbol or ''}|{biz_key}"
    return "evt_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def build_event(
    *,
    provider: str,
    data_type: str,
    symbol: str | None,
    market: str,
    biz_key: str,
    event_time: Any,
    content: dict[str, Any],
    source_info: dict[str, Any] | None = None,
    schema_version: str = SCHEMA_VERSION,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Assemble one contract-v0.2 envelope. ``event_time`` is business time."""
    key = str(biz_key)
    info: dict[str, Any] = {"provider": provider}
    for name, value in (source_info or {}).items():
        if value is not None and value != "":
            info[name] = value
    info.setdefault("crawl_time", _iso(_utc_now()))
    body = dict(content or {})
    body.setdefault("quality_metrics", {"confidence": "high", "quality_status": "normal"})
    event: dict[str, Any] = {
        "schema_version": schema_version,
        "event_id": event_id(provider, data_type, symbol, key),
        "biz_key": key,
        "event_time": _iso(_parse_time(event_time)),
        "data_type": data_type,
        "symbol": symbol or None,
        "market": market,
        "source_info": info,
        "content": body,
    }
    if trace_id:
        event["trace_id"] = trace_id
    return event


class EventPublisher:
    """Bounded, failure-isolated publisher with a SQLite outbox."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        redis_url: str | None = None,
        outbox_path: str | Path | None = None,
    ) -> None:
        settings = get_settings()
        self.enabled = settings.event_publish_enable if enabled is None else bool(enabled)
        self.redis_url = redis_url or settings.fc_event_redis_url
        self.outbox_path = Path(outbox_path or (Path(settings.archive_dir) / "event_outbox.db"))
        self.published_total = 0
        self.publish_failed_total = 0
        self._client: Any = None
        self._connect_backoff_until = 0.0
        self._schema_ready = False
        self._last_error = ""
        self._aged_alert_at = 0.0
        self._heartbeat_pruned_at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ connection

    def _client_or_none(self) -> Any:
        with self._lock:
            if self._client is not None:
                return self._client
            if time.time() < self._connect_backoff_until:
                return None
            try:
                import redis

                client = redis.Redis.from_url(
                    self.redis_url,
                    socket_connect_timeout=1.2,
                    socket_timeout=PUBLISH_TIMEOUT_SECONDS,
                    decode_responses=True,
                )
                client.ping()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"[:200]
                self._connect_backoff_until = time.time() + CONNECT_BACKOFF_SECONDS
                return None
            self._client = client
            self._connect_backoff_until = 0.0
            return client

    def _deliver(self, payload: str) -> bool:
        client = self._client_or_none()
        if client is None:
            return False
        try:
            client.xadd(STREAM_KEY, {"payload": payload}, maxlen=MAXLEN, approximate=True)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"[:200]
            with self._lock:
                self._client = None
                self._connect_backoff_until = time.time() + CONNECT_BACKOFF_SECONDS
            return False
        self._last_error = ""
        return True

    # --------------------------------------------------------------- outbox

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.outbox_path, timeout=10)
        try:
            if not self._schema_ready:
                con.executescript(OUTBOX_SCHEMA)
                self._schema_ready = True
            yield con
            con.commit()
        finally:
            con.close()

    def _enqueue(self, event_id_value: str, payload: str, error: str) -> None:
        stamp = _iso(_utc_now())
        try:
            with self._db() as con:
                con.execute(
                    "INSERT OR IGNORE INTO event_outbox"
                    " (event_id, payload, created_at, attempts, next_retry_at, last_error)"
                    " VALUES (?,?,?,0,?,?)",
                    (event_id_value, payload, stamp, stamp, error[:500]),
                )
        except Exception as exc:  # the last thing that may fail must not break the crawl
            _log().error(
                "event outbox write failed",
                extra={"event": "event_outbox_write_failed", "error_code": "QFSD-2001", "error": str(exc)[:200]},
            )

    def _aged_pending(self, con: sqlite3.Connection) -> int:
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=AGED_AFTER_SECONDS)
        row = con.execute("SELECT COUNT(*) FROM event_outbox WHERE created_at < ?", (_iso(cutoff),)).fetchone()
        return int(row[0] if row else 0)

    def _claim_heartbeat(self, event_id_value: str) -> bool:
        """True the first time a heartbeat id is seen, restarts included.

        A run_id names one round, so re-running a finished round is the same
        fact the consumer would drop by event_id; sending it again would only
        grow the stream. The claim lives next to the outbox so a manual re-run
        after a restart is deduped too. If the claim store is unusable, send
        anyway: a local disk problem must not suppress liveness.
        """
        try:
            with self._db() as con:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS event_heartbeat_sent"
                    " (event_id TEXT PRIMARY KEY, sent_at TEXT NOT NULL)"
                )
                cur = con.execute(
                    "INSERT OR IGNORE INTO event_heartbeat_sent (event_id, sent_at) VALUES (?,?)",
                    (event_id_value, _iso(_utc_now())),
                )
                claimed = cur.rowcount == 1
                now = time.time()
                if claimed and now - self._heartbeat_pruned_at >= AGED_ALERT_INTERVAL:
                    self._heartbeat_pruned_at = now
                    cutoff = _iso(_utc_now() - _dt.timedelta(days=7))
                    con.execute("DELETE FROM event_heartbeat_sent WHERE sent_at < ?", (cutoff,))
        except Exception as exc:
            self._last_error = f"heartbeat claim: {exc}"[:200]
            return True
        return claimed

    # ------------------------------------------------------------- publish

    def publish(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            payload = json.dumps(event, ensure_ascii=False, default=str)
        except Exception as exc:
            with self._lock:
                self.publish_failed_total += 1
                self._last_error = f"serialize: {exc}"[:200]
            _log().warning(
                "event serialize failed",
                extra={"event": "event_serialize_failed", "error_code": "QFSD-1001", "error": str(exc)[:200]},
            )
            return
        if self._deliver(payload):
            with self._lock:
                self.published_total += 1
            return
        with self._lock:
            self.publish_failed_total += 1
        key = str(event.get("event_id") or "")
        if key:
            self._enqueue(key, payload, self._last_error)
        _log().warning(
            "event delivery failed, parked in outbox",
            extra={
                "event": "event_publish_failed",
                "error_code": "QFSD-2001",
                "detail": {"event_id": key, "data_type": event.get("data_type")},
            },
        )

    def flush(self, limit: int = 100) -> int:
        """Re-deliver due outbox rows; returns how many reached the stream."""
        if not self.enabled:
            return 0
        delivered = 0
        try:
            with self._db() as con:
                rows = con.execute(
                    "SELECT event_id, payload, attempts FROM event_outbox"
                    " WHERE next_retry_at <= ? ORDER BY next_retry_at LIMIT ?",
                    (_iso(_utc_now()), max(1, int(limit))),
                ).fetchall()
                for key, payload, attempts in rows:
                    if self._deliver(payload):
                        con.execute("DELETE FROM event_outbox WHERE event_id=?", (key,))
                        delivered += 1
                        with self._lock:
                            self.published_total += 1
                        continue
                    step = min(int(attempts or 0), len(BACKOFF_SECONDS) - 1)
                    retry_at = _iso(_utc_now() + _dt.timedelta(seconds=BACKOFF_SECONDS[step]))
                    con.execute(
                        "UPDATE event_outbox SET attempts=attempts+1, next_retry_at=?, last_error=?"
                        " WHERE event_id=?",
                        (retry_at, self._last_error[:500], key),
                    )
                    if self._client is None:
                        # Redis is unreachable: defer the rest of the batch to
                        # the next tick instead of paying the timeout per row.
                        break
                aged = self._aged_pending(con)
        except Exception as exc:
            self._last_error = f"outbox: {exc}"[:200]
            _log().warning(
                "event outbox flush failed",
                extra={"event": "event_outbox_flush_failed", "error_code": "QFSD-2001", "error": str(exc)[:200]},
            )
            return delivered
        now = time.time()
        if aged and now - self._aged_alert_at >= AGED_ALERT_INTERVAL:
            self._aged_alert_at = now
            _log().error(
                "event outbox has entries older than 24h",
                extra={"event": "event_outbox_aged", "error_code": "QFSD-2001", "detail": {"count": aged}},
            )
        return delivered

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enabled": self.enabled,
            "stream": STREAM_KEY,
            "redis": _redact(self.redis_url),
            "outbox_pending": 0,
            "outbox_oldest_seconds": 0,
            "published_total": self.published_total,
            "publish_failed_total": self.publish_failed_total,
            "last_error": self._last_error,
        }
        # Do not create the outbox file just because someone asked for status.
        if not self.outbox_path.exists():
            return out
        try:
            with self._db() as con:
                row = con.execute("SELECT COUNT(*), MIN(created_at) FROM event_outbox").fetchone()
            out["outbox_pending"] = int(row[0] if row else 0)
            oldest = row[1] if row else None
            if oldest:
                delta = _utc_now() - _parse_time(oldest)
                out["outbox_oldest_seconds"] = max(0, int(delta.total_seconds()))
        except Exception as exc:
            out["last_error"] = f"outbox: {exc}"[:200]
        return out


_PUBLISHER: EventPublisher | None = None
_PUBLISHER_LOCK = threading.Lock()


def get_publisher() -> EventPublisher:
    global _PUBLISHER
    with _PUBLISHER_LOCK:
        if _PUBLISHER is None:
            _PUBLISHER = EventPublisher()
        return _PUBLISHER


def reset_publisher() -> None:
    global _PUBLISHER
    with _PUBLISHER_LOCK:
        _PUBLISHER = None


def is_enabled() -> bool:
    return get_publisher().enabled


def publish(event: dict[str, Any]) -> None:
    get_publisher().publish(event)


def flush_outbox(limit: int = 100) -> int:
    return get_publisher().flush(limit)


def stats() -> dict[str, Any]:
    return get_publisher().stats()


def heartbeat(
    *,
    provider: str,
    market: str,
    run_id: str,
    status: str,
    items: int = 0,
    started_at: Any = None,
    finished_at: Any = None,
    next_run_at: Any = None,
    trace_id: str | None = None,
) -> None:
    """Liveness event. ``run_id`` is the biz_key, so one round yields one id."""
    if not is_enabled():
        return
    try:
        finished = _parse_time(finished_at) if finished_at is not None else _utc_now()
        fields: dict[str, Any] = {
            "run_id": run_id,
            "status": status,
            "items_collected": int(items or 0),
            "finished_at": _iso(finished),
        }
        if started_at is not None:
            fields["started_at"] = _iso(_parse_time(started_at))
        if next_run_at is not None:
            fields["next_run_at"] = _iso(_parse_time(next_run_at))
        event = build_event(
            provider=provider,
            data_type="heartbeat",
            symbol=None,
            market=market,
            biz_key=run_id,
            event_time=finished,
            content={
                "structured_fields": fields,
                "quality_metrics": {"confidence": "high", "quality_status": "normal"},
            },
            trace_id=trace_id,
        )
    except Exception as exc:
        _log().warning(
            "heartbeat build failed",
            extra={"event": "event_heartbeat_failed", "error_code": "QFSD-1001", "error": str(exc)[:200]},
        )
        return
    if not get_publisher()._claim_heartbeat(str(event["event_id"])):
        return
    publish(event)


__all__ = [
    "BACKOFF_SECONDS",
    "EventPublisher",
    "MAXLEN",
    "OUTBOX_SCHEMA",
    "STREAM_KEY",
    "build_event",
    "event_id",
    "flush_outbox",
    "get_publisher",
    "heartbeat",
    "is_enabled",
    "publish",
    "reset_publisher",
    "stats",
]
