"""One-off history backfill into the FinanceCoffer event stream (spec T-104).

Reads the four crawl tables already in the local archive and republishes every
row through the T-101 publisher. The envelope is assembled by the same helpers
``crawler.store`` uses on the live path, so a backfilled row hashes to the same
``event_id`` as it would have in real time; the only additions are
``source_info.backfill=true`` and the archived ``crawl_time``.

Idempotency has two layers. The consumer drops events it has already seen by
``event_id``, and this script keeps a ledger of every id it delivered
(``event_backfill_sent``, next to the outbox) so a re-run does not grow the
stream. Deliveries that fail because Redis is down land in the same SQLite
outbox the daemon uses; the outbox is drained before and after the scan, and
once Redis comes back the remaining rows are sent exactly once.

The archive is opened read-only. The script is a one-off: it is not wired into
the daemon and stops when the process exits.

Usage:
    python scripts/backfill_cn.py --dry-run
    python scripts/backfill_cn.py --since 2024-01-01 --until 2026-10-25
    python scripts/backfill_cn.py --db var/archive/tickhub.db --batch-size 1000
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tickhub_cn.core.config import get_settings  # noqa: E402
from tickhub_cn.crawler import event_publisher, store  # noqa: E402

# table -> business-date column used by --since/--until
TABLES = (
    ("announcement", "publish_time"),
    ("research_report", "publish_time"),
    ("risk_event", "event_date"),
    ("northbound_flow", "trade_date"),
)

# table -> archived column holding the original crawl time. Northbound rows are
# snapshots and never had a crawl_time column of their own.
CRAWL_TIME_COLUMNS = {
    "announcement": "crawl_time",
    "research_report": "crawl_time",
    "risk_event": "crawl_time",
    "northbound_flow": "snapshot_at",
}

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_backfill_sent (
  event_id   TEXT PRIMARY KEY,
  source     TEXT NOT NULL,
  sent_at    TEXT NOT NULL
);
"""

FLUSH_BATCH = 1000
FLUSH_PASSES = 100


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _rfc3339(value: Any) -> str:
    """Archived crawl_time as RFC3339 UTC; unreadable values stay empty.

    The archive writes local wall-clock time (``store._now``), so a naive value
    is pinned to Asia/Shanghai the same way the live path pins business times.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=store._CN_TZ)
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _value(row: sqlite3.Row, name: str) -> Any:
    """Tolerant column read: an older archive may predate a migration."""
    try:
        return row[name]
    except IndexError:
        return None


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    raw = _value(row, "payload")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ assembly
#
# Row -> envelope goes through the very helpers the live hooks call. Any field
# mapping here would be a second implementation of the contract and could drift
# from the event_id the consumer already deduplicated against.


def _announcement_event(row: sqlite3.Row) -> dict[str, Any]:
    data = _payload(row)
    code = _text(row["code"])
    title = _text(row["title"])
    published = _text(row["publish_time"])
    url = row["url"]
    if code:
        data["code"] = code
    if title:
        data["title"] = title
    if published:
        data["publish_time"] = published
    if url:
        data["url"] = url
    provider = _text(row["source"]) or "cninfo"
    return store._announcement_event(
        provider,
        _text(row["item_id"]),
        data,
        code=code,
        title=title,
        published=published,
        url=url,
    )


def _report_event(row: sqlite3.Row) -> dict[str, Any]:
    data = _payload(row)
    code = _text(row["code"])
    title = _text(row["title"])
    published = _text(row["publish_time"])
    if code:
        data["code"] = code
    if title:
        data["title"] = title
    if published:
        data["publish_date"] = published
    if _text(row["org"]):
        data["org"] = _text(row["org"])
    provider = _text(row["source"]) or "eastmoney"
    biz_key = store._report_key(data, title=title, published=published)
    return store._report_event(
        provider, data, code=code, title=title, published=published, biz_key=biz_key
    )


def _risk_event(row: sqlite3.Row) -> dict[str, Any]:
    data = _payload(row)
    code = _text(row["code"])
    risk_type = _text(row["type"])
    occurred = _text(row["event_date"])
    title = _text(row["title"])
    source = _text(row["source"])
    if code:
        data["code"] = code
    if risk_type:
        data["type"] = risk_type
    if occurred:
        data["date"] = occurred
    if title:
        data["title"] = title
    if _text(row["severity"]):
        data["severity"] = _text(row["severity"])
    if source:
        data["source"] = source
    return store._risk_event(
        source or "computed",
        _text(row["item_id"]),
        data,
        code=code,
        risk_type=risk_type,
        occurred=occurred,
        title=title,
    )


def _northbound_event(row: sqlite3.Row) -> dict[str, Any]:
    day = _text(_value(row, "trade_date"))
    channel = _text(_value(row, "channel"))
    if not day or not channel:
        raise ValueError("northbound row without trade_date/channel has no business key")
    data = _payload(row)
    data["name"] = _text(_value(row, "name")) or _text(data.get("name"))
    data["channel"] = channel
    data["trade_date"] = day
    for column in ("net_inflow", "deal_amount_wan", "deal_num", "quota_text"):
        value = _value(row, column)
        if value is not None:
            data[column] = value
    return store._northbound_event(day, channel, data)


_BUILDERS = {
    "announcement": _announcement_event,
    "research_report": _report_event,
    "risk_event": _risk_event,
    "northbound_flow": _northbound_event,
}


def _decorate(event: dict[str, Any], crawl_time: Any) -> dict[str, Any]:
    info = event.setdefault("source_info", {})
    info["backfill"] = True
    # The contract allows crawl_time to be omitted, but a fabricated "now" would
    # make historical rows look freshly crawled. Keep the archive value, or an
    # explicit empty string when the archive never recorded one.
    info["crawl_time"] = _rfc3339(crawl_time)
    return event


def _invalid_reason(table: str, event: dict[str, Any]) -> str:
    if not event.get("event_id"):
        return "missing event_id"
    if not event.get("biz_key"):
        return "missing biz_key"
    if table != "northbound_flow" and not event.get("symbol"):
        return "missing symbol"
    return ""


# -------------------------------------------------------------------- ledger


class Ledger:
    """event_id -> already delivered by a previous backfill run."""

    def __init__(self, path: Path, *, writable: bool) -> None:
        self.path = path
        self.writable = writable
        self.sent: set[str] = set()
        self._pending: list[tuple[str, str, str]] = []
        if not path.exists():
            return
        con = sqlite3.connect(path, timeout=10)
        try:
            try:
                self.sent.update(row[0] for row in con.execute("SELECT event_id FROM event_backfill_sent"))
            except sqlite3.OperationalError:
                pass
        finally:
            con.close()

    def mark(self, event_id: str, source: str) -> None:
        if event_id in self.sent:
            return
        self.sent.add(event_id)
        if self.writable:
            self._pending.append((event_id, source, _now_iso()))

    def flush(self) -> None:
        if not self._pending:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=10)
        try:
            con.executescript(LEDGER_SCHEMA)
            con.executemany(
                "INSERT OR IGNORE INTO event_backfill_sent (event_id, source, sent_at) VALUES (?,?,?)",
                self._pending,
            )
            con.commit()
        finally:
            con.close()
        self._pending.clear()


def _outbox_pending(path: Path) -> set[str]:
    if not path.exists():
        return set()
    con = sqlite3.connect(path, timeout=10)
    try:
        try:
            return {row[0] for row in con.execute("SELECT event_id FROM event_outbox")}
        except sqlite3.OperationalError:
            return set()
    finally:
        con.close()


def _drop_outbox(path: Path, event_ids: list[str]) -> None:
    """A direct delivery supersedes a row parked by an earlier failed attempt.

    Without this the stale row would be flushed again later and append the same
    event to the stream twice (the consumer would drop it, but XLEN would grow).
    """
    if not event_ids or not path.exists():
        return
    con = sqlite3.connect(path, timeout=10)
    try:
        try:
            for start in range(0, len(event_ids), 500):
                chunk = event_ids[start : start + 500]
                marks = ",".join("?" for _ in chunk)
                con.execute(f"DELETE FROM event_outbox WHERE event_id IN ({marks})", chunk)
            con.commit()
        except sqlite3.OperationalError:
            con.rollback()
    finally:
        con.close()


def _drain_outbox(publisher: event_publisher.EventPublisher, ledger: Ledger) -> tuple[int, int]:
    """Send due outbox rows; returns (delivered, still_pending)."""
    before = _outbox_pending(publisher.outbox_path)
    if not before:
        return 0, 0
    delivered = 0
    for _ in range(FLUSH_PASSES):
        count = publisher.flush(FLUSH_BATCH)
        delivered += count
        if count < FLUSH_BATCH:
            break
    after = _outbox_pending(publisher.outbox_path)
    for event_id in before - after:
        ledger.mark(event_id, "outbox")
    ledger.flush()
    return delivered, len(after)


# ---------------------------------------------------------------------- scan


def _in_window(day: str, since: str, until: str) -> bool:
    if not day:
        return not (since or until)
    if since and day < since:
        return False
    if until and day > until:
        return False
    return True


def _new_stats() -> dict[str, int]:
    return {
        "read": 0,
        "filtered": 0,
        "skipped": 0,
        "published": 0,
        "outbox": 0,
        "would_publish": 0,
        "invalid": 0,
        "errors": 0,
    }


def _scan_table(
    con: sqlite3.Connection,
    table: str,
    date_column: str,
    *,
    batch_size: int,
    since: str,
    until: str,
    dry_run: bool,
    publisher: event_publisher.EventPublisher | None,
    ledger: Ledger,
    stats: dict[str, int],
) -> None:
    builder = _BUILDERS[table]
    last_rowid = 0
    while True:
        rows = con.execute(
            f"SELECT rowid AS _rowid, * FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (last_rowid, batch_size),
        ).fetchall()
        if not rows:
            break
        last_rowid = int(rows[-1]["_rowid"])
        delivered_ids: list[str] = []
        for row in rows:
            stats["read"] += 1
            day = store._business_date(row[date_column])
            if not _in_window(day, since, until):
                stats["filtered"] += 1
                continue
            try:
                event = _decorate(builder(row), _value(row, CRAWL_TIME_COLUMNS[table]))
            except Exception as exc:  # a bad row must not abort the whole backfill
                stats["errors"] += 1
                print(f"  ! {table} rowid={row['_rowid']}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            reason = _invalid_reason(table, event)
            if reason:
                stats["invalid"] += 1
                print(f"  ! {table} rowid={row['_rowid']}: {reason}", file=sys.stderr)
                continue
            event_id = str(event["event_id"])
            if event_id in ledger.sent:
                stats["skipped"] += 1
                continue
            if dry_run:
                stats["would_publish"] += 1
                continue
            assert publisher is not None
            published_before = publisher.published_total
            publisher.publish(event)
            if publisher.published_total > published_before:
                stats["published"] += 1
                ledger.mark(event_id, table)
                delivered_ids.append(event_id)
            else:
                # publish() is documented never to raise; anything it could not
                # deliver is parked in the outbox keyed by event_id.
                stats["outbox"] += 1
        ledger.flush()
        if publisher is not None and delivered_ids:
            _drop_outbox(publisher.outbox_path, delivered_ids)


def _parse_day(value: str, flag: str) -> str:
    day = store._business_date(value)
    if not day:
        raise argparse.ArgumentTypeError(f"{flag} must be a date (YYYY-MM-DD), got {value!r}")
    return day


def _parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Backfill archived CN rows into the FinanceCoffer Redis Stream (one-off).",
    )
    parser.add_argument("--db", default=settings.archive_db_path, help="archive SQLite path (read-only)")
    parser.add_argument("--batch-size", type=int, default=500, help="rows per page (default 500)")
    parser.add_argument("--since", default="", help="include business dates on/after YYYY-MM-DD")
    parser.add_argument("--until", default="", help="include business dates on/before YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="count only; never touch Redis or the outbox")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        since = _parse_day(args.since, "--since") if args.since else ""
        until = _parse_day(args.until, "--until") if args.until else ""
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if since and until and since > until:
        parser.error("--since is after --until")
    if args.batch_size < 1:
        parser.error("--batch-size must be >= 1")

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"archive not found: {db_path}", file=sys.stderr)
        return 1

    settings = get_settings()
    publisher: event_publisher.EventPublisher | None = None
    ledger = Ledger(Path(settings.archive_dir) / "event_outbox.db", writable=not args.dry_run)
    if not args.dry_run:
        if not settings.event_publish_enable:
            print(
                "EVENT_PUBLISH_ENABLE is false; publishing is off. Set it to true for a real "
                "backfill, or pass --dry-run to only count.",
                file=sys.stderr,
            )
            return 2
        publisher = event_publisher.EventPublisher()

    print(f"archive : {db_path}")
    print(f"outbox  : {ledger.path}")
    if args.dry_run:
        print("mode    : dry-run (nothing is sent)")
    else:
        assert publisher is not None
        print(f"stream  : {event_publisher.STREAM_KEY} @ {publisher.redis_url}")

    uri = "file:" + db_path.resolve().as_posix() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    delivered_from_outbox = 0
    pending_end = 0
    try:
        present = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if publisher is not None:
            delivered_from_outbox, _ = _drain_outbox(publisher, ledger)

        total = _new_stats()
        per_table: list[tuple[str, dict[str, int]]] = []
        for table, date_column in TABLES:
            if table not in present:
                per_table.append((table, {**_new_stats(), "missing": 1}))  # type: ignore[dict-item]
                continue
            stats = _new_stats()
            _scan_table(
                con,
                table,
                date_column,
                batch_size=args.batch_size,
                since=since,
                until=until,
                dry_run=args.dry_run,
                publisher=publisher,
                ledger=ledger,
                stats=stats,
            )
            per_table.append((table, stats))
            for key, value in stats.items():
                total[key] += value

        if publisher is not None:
            more, pending_end = _drain_outbox(publisher, ledger)
            delivered_from_outbox += more
    finally:
        con.close()

    header = f"{'table':<18}{'read':>8}{'window':>8}{'published':>11}{'outbox':>8}{'skipped':>9}{'invalid':>9}{'errors':>8}"
    print()
    print(header)
    print("-" * len(header))
    for table, stats in per_table:
        if stats.get("missing"):
            print(f"{table:<18}{'missing':>8}")
            continue
        window = stats["read"] - stats["filtered"]
        published = stats["would_publish"] if args.dry_run else stats["published"]
        print(
            f"{table:<18}{stats['read']:>8}{window:>8}{published:>11}"
            f"{stats['outbox']:>8}{stats['skipped']:>9}{stats['invalid']:>9}{stats['errors']:>8}"
        )
    print("-" * len(header))
    window_total = total["read"] - total["filtered"]
    published_total = total["would_publish"] if args.dry_run else total["published"]
    print(
        f"{'total':<18}{total['read']:>8}{window_total:>8}{published_total:>11}"
        f"{total['outbox']:>8}{total['skipped']:>9}{total['invalid']:>9}{total['errors']:>8}"
    )
    if not args.dry_run:
        print(
            f"outbox drained this run: {delivered_from_outbox}; still pending: {pending_end}"
        )
    if args.dry_run:
        print("dry-run: choose a real run (EVENT_PUBLISH_ENABLE=true) to send these events.")
    ledger.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
