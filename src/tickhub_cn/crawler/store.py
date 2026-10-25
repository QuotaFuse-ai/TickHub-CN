"""Local archive backing the offline crawler.

SQLite by default (zero setup, ships with Python). The schema mirrors
``docs/DATA_MODEL.md``; tables the crawler does not fill yet are still created
so operators and downstream jobs see a stable shape. Writes are idempotent so
a re-run never duplicates an announcement or a report.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..core.config import get_settings
from ..logging.setup import get_logger

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_basic (
    code TEXT PRIMARY KEY, name TEXT, industry TEXT, market TEXT,
    payload TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS kline (
    code TEXT, period TEXT, adjust TEXT, trade_date TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
    payload TEXT, crawl_time TEXT,
    PRIMARY KEY (code, period, adjust, trade_date)
);
CREATE TABLE IF NOT EXISTS financial_report (
    code TEXT, report_date TEXT, statement TEXT, payload TEXT, crawl_time TEXT,
    PRIMARY KEY (code, report_date, statement)
);
CREATE TABLE IF NOT EXISTS shareholder (
    code TEXT, report_date TEXT, payload TEXT, crawl_time TEXT,
    PRIMARY KEY (code, report_date)
);
CREATE TABLE IF NOT EXISTS announcement (
    item_id TEXT PRIMARY KEY, code TEXT, title TEXT, publish_time TEXT,
    url TEXT, source TEXT, crawl_time TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_announcement_code_time ON announcement(code, publish_time DESC);
CREATE TABLE IF NOT EXISTS research_report (
    item_id TEXT PRIMARY KEY, code TEXT, title TEXT, publish_time TEXT,
    org TEXT, analyst TEXT, rating TEXT, url TEXT, source TEXT,
    crawl_time TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_report_code_time ON research_report(code, publish_time DESC);
CREATE TABLE IF NOT EXISTS risk_event (
    item_id TEXT PRIMARY KEY, code TEXT, type TEXT, severity TEXT, event_date TEXT,
    title TEXT, source TEXT, crawl_time TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_risk_code ON risk_event(code, event_date DESC);
CREATE TABLE IF NOT EXISTS northbound_flow (
    item_id TEXT PRIMARY KEY, trade_date TEXT, channel TEXT, name TEXT,
    net_inflow REAL, net_inflow_wan REAL, status INTEGER, source TEXT,
    snapshot_at TEXT, payload TEXT,
    deal_amount_wan REAL, deal_num INTEGER, quota_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_northbound_date ON northbound_flow(trade_date DESC, channel);
CREATE TABLE IF NOT EXISTS crawl_task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT, started_at TEXT, finished_at TEXT, status TEXT,
    fetched INTEGER DEFAULT 0, inserted INTEGER DEFAULT 0, error TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY, ok INTEGER, checked_at TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS proxy_node (
    id TEXT PRIMARY KEY, payload TEXT, checked_at TEXT
);
CREATE TABLE IF NOT EXISTS log_entry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, level TEXT, logger TEXT, message TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, action TEXT, actor TEXT, detail TEXT
);
"""

ITEM_FIELDS = ("id", "announcementId", "info_code", "infoCode", "art_code", "ann_id")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` never
# touches an existing file, so each one is added in place on open.
_MIGRATIONS = {
    "northbound_flow": (
        ("deal_amount_wan", "REAL"),
        ("deal_num", "INTEGER"),
        ("quota_text", "TEXT"),
    ),
}


def _item_id(source: str, row: dict[str, Any], *fallback: Any) -> str:
    for key in ITEM_FIELDS:
        value = row.get(key)
        if value:
            return f"{source}:{value}"
    blob = "|".join(str(x or "") for x in fallback) or json.dumps(row, sort_keys=True, default=str)
    return f"{source}:{hashlib.sha1(blob.encode('utf-8')).hexdigest()[:20]}"


class Archive:
    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self.path = Path(path or settings.archive_db_path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _ensure(self) -> None:
        with self._lock, self.connect() as con:
            con.executescript(SCHEMA)
            for table, columns in _MIGRATIONS.items():
                have = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
                for name, kind in columns:
                    if name not in have:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    # ----------------------------------------------------------------- writes

    def insert_announcements(self, rows: list[dict[str, Any]], *, source: str = "cninfo") -> int:
        inserted = 0
        stamp = _now()
        with self._lock, self.connect() as con:
            for row in rows or []:
                code = str(row.get("code") or row.get("stockCode") or "")
                title = str(row.get("title") or row.get("announcementTitle") or "")
                published = str(
                    row.get("publish_time")
                    or row.get("date")
                    or row.get("announcementTime")
                    or row.get("NOTICE_DATE")
                    or ""
                )
                url = row.get("url") or row.get("adjunctUrl") or row.get("pdf")
                item_id = _item_id(source, row, code, title, published)
                cur = con.execute(
                    "INSERT OR IGNORE INTO announcement"
                    " (item_id, code, title, publish_time, url, source, crawl_time, payload)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (item_id, code, title, published, url, source, stamp, json.dumps(row, ensure_ascii=False, default=str)),
                )
                inserted += int(cur.rowcount or 0)
        return inserted

    def insert_reports(self, rows: list[dict[str, Any]], *, source: str = "eastmoney") -> int:
        inserted = 0
        stamp = _now()
        with self._lock, self.connect() as con:
            for row in rows or []:
                code = str(row.get("code") or "")
                title = str(row.get("title") or "")
                published = str(row.get("publish_date") or row.get("publishTime") or "")
                item_id = _item_id(source, row, code, title, published, row.get("org"))
                cur = con.execute(
                    "INSERT OR IGNORE INTO research_report"
                    " (item_id, code, title, publish_time, org, analyst, rating, url, source, crawl_time, payload)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item_id,
                        code,
                        title,
                        published,
                        row.get("org"),
                        row.get("analyst"),
                        row.get("rating"),
                        row.get("pdf") or row.get("url"),
                        source,
                        stamp,
                        json.dumps(row, ensure_ascii=False, default=str),
                    ),
                )
                inserted += int(cur.rowcount or 0)
        return inserted

    def insert_risks(self, rows: list[dict[str, Any]], *, source: str = "computed") -> int:
        inserted = 0
        stamp = _now()
        with self._lock, self.connect() as con:
            for row in rows or []:
                item_id = _item_id(source, row, row.get("code"), row.get("type"), row.get("title"), row.get("date"))
                cur = con.execute(
                    "INSERT OR IGNORE INTO risk_event"
                    " (item_id, code, type, severity, event_date, title, source, crawl_time, payload)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        item_id,
                        str(row.get("code") or ""),
                        str(row.get("type") or ""),
                        str(row.get("severity") or ""),
                        str(row.get("date") or ""),
                        str(row.get("title") or ""),
                        str(row.get("source") or source),
                        stamp,
                        json.dumps(row, ensure_ascii=False, default=str),
                    ),
                )
                inserted += int(cur.rowcount or 0)
        return inserted

    def insert_northbound(
        self,
        rows: list[dict[str, Any]],
        *,
        trade_date: str,
        source: str = "eastmoney",
        snapshot_at: str = "",
    ) -> int:
        """Upsert one channel snapshot; returns how many rows were new.

        A row is identified by (trade_date, channel), so the intraday snapshot
        and the datacenter history backfill for the same day enrich each other
        instead of producing duplicates: the snapshot supplies the net inflow,
        the history supplies turnover and trade count. A later value only
        replaces a column when it is not null, which keeps a backfill from
        wiping the close-of-day flow (and vice versa).
        """
        inserted = 0
        with self._lock, self.connect() as con:
            for row in rows or []:
                channel = str(row.get("channel") or "")
                day = str(row.get("trade_date") or trade_date or "")
                if not channel or not day:
                    continue
                item_id = f"eastmoney:{day}:{channel}"
                existing = con.execute(
                    "SELECT source, payload FROM northbound_flow WHERE item_id=?", (item_id,)
                ).fetchone()
                sources = [s for s in str((existing["source"] if existing else "") or "").split(",") if s]
                if source not in sources:
                    sources.append(source)
                merged: dict[str, Any] = {}
                if existing:
                    try:
                        old = json.loads(existing["payload"] or "{}")
                    except (TypeError, ValueError):
                        old = {}
                    if isinstance(old, dict):
                        merged.update(old)
                merged.update({k: v for k, v in row.items() if v is not None})
                merged["channel"] = channel
                merged["name"] = str(row.get("name") or merged.get("name") or "")
                merged["trade_date"] = day
                con.execute(
                    "INSERT INTO northbound_flow"
                    " (item_id, trade_date, channel, name, net_inflow, net_inflow_wan, status,"
                    "  source, snapshot_at, payload, deal_amount_wan, deal_num, quota_text)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(item_id) DO UPDATE SET"
                    "  name=COALESCE(NULLIF(excluded.name, ''), northbound_flow.name),"
                    "  net_inflow=COALESCE(excluded.net_inflow, northbound_flow.net_inflow),"
                    "  net_inflow_wan=COALESCE(excluded.net_inflow_wan, northbound_flow.net_inflow_wan),"
                    "  status=COALESCE(excluded.status, northbound_flow.status),"
                    "  deal_amount_wan=COALESCE(excluded.deal_amount_wan, northbound_flow.deal_amount_wan),"
                    "  deal_num=COALESCE(excluded.deal_num, northbound_flow.deal_num),"
                    "  quota_text=COALESCE(NULLIF(excluded.quota_text, ''), northbound_flow.quota_text),"
                    "  source=excluded.source,"
                    "  snapshot_at=CASE WHEN excluded.snapshot_at <> ''"
                    "    THEN excluded.snapshot_at ELSE northbound_flow.snapshot_at END,"
                    "  payload=excluded.payload",
                    (
                        item_id,
                        day,
                        channel,
                        merged["name"],
                        row.get("net_inflow"),
                        row.get("net_inflow_wan"),
                        row.get("status"),
                        ",".join(sources),
                        snapshot_at or str(row.get("snapshot_at") or ""),
                        json.dumps(merged, ensure_ascii=False, default=str),
                        row.get("deal_amount_wan"),
                        row.get("deal_num"),
                        row.get("quota_text"),
                    ),
                )
                inserted += int(existing is None)
        return inserted

    def start_task(self, task: str, detail: dict[str, Any] | None = None) -> int:
        with self._lock, self.connect() as con:
            cur = con.execute(
                "INSERT INTO crawl_task (task, started_at, status, detail) VALUES (?,?,?,?)",
                (task, _now(), "running", json.dumps(detail or {}, ensure_ascii=False)),
            )
            return int(cur.lastrowid or 0)

    def finish_task(
        self,
        task_id: int,
        *,
        status: str,
        fetched: int = 0,
        inserted: int = 0,
        error: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                "UPDATE crawl_task SET finished_at=?, status=?, fetched=?, inserted=?, error=?,"
                " detail=COALESCE(?, detail) WHERE id=?",
                (
                    _now(),
                    status,
                    int(fetched),
                    int(inserted),
                    error[:500],
                    json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                    task_id,
                ),
            )

    # ------------------------------------------------------------------ reads

    def query(self, table: str, *, code: str = "", limit: int = 50, order: str = "crawl_time") -> list[dict[str, Any]]:
        allowed = {"announcement", "research_report", "risk_event", "crawl_task", "northbound_flow"}
        if table not in allowed:
            raise ValueError(f"unsupported archive table: {table}")
        sql = f"SELECT * FROM {table}"
        params: list[Any] = []
        if code:
            column = {"crawl_task": "detail", "northbound_flow": "channel"}.get(table, "code")
            sql += f" WHERE {column} LIKE ?"
            params.append(f"%{code}%")
        sql += f" ORDER BY {order} DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self.connect() as con:
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        for row in rows:
            if "payload" in row and isinstance(row["payload"], str):
                try:
                    row["payload"] = json.loads(row["payload"])
                except Exception:
                    pass
        return rows

    def announcements(self, code: str = "", limit: int = 50) -> list[dict[str, Any]]:
        return self.query("announcement", code=code, limit=limit, order="publish_time")

    def reports(self, code: str = "", limit: int = 50) -> list[dict[str, Any]]:
        return self.query("research_report", code=code, limit=limit, order="publish_time")

    def risks(self, code: str = "", limit: int = 200) -> list[dict[str, Any]]:
        return self.query("risk_event", code=code, limit=limit, order="event_date")

    def northbound(self, trade_date: str = "", limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM northbound_flow"
        params: list[Any] = []
        if trade_date:
            sql += " WHERE trade_date=?"
            params.append(trade_date)
        sql += " ORDER BY trade_date DESC, channel LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    def tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.query("crawl_task", limit=limit, order="started_at")

    def last_task(self, task: str = "") -> dict[str, Any] | None:
        sql = "SELECT * FROM crawl_task"
        params: list[Any] = []
        if task:
            sql += " WHERE task=?"
            params.append(task)
        sql += " ORDER BY id DESC LIMIT 1"
        with self.connect() as con:
            row = con.execute(sql, params).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict[str, Any]:
        tables = ("announcement", "research_report", "risk_event", "northbound_flow", "crawl_task")
        out: dict[str, Any] = {"path": str(self.path), "size_kb": 0}
        try:
            out["size_kb"] = round(self.path.stat().st_size / 1024, 1)
        except OSError:
            pass
        with self.connect() as con:
            for table in tables:
                out[table] = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return out


_ARCHIVE: Archive | None = None
_LOCK = threading.Lock()


def get_archive() -> Archive:
    global _ARCHIVE
    with _LOCK:
        if _ARCHIVE is None:
            try:
                _ARCHIVE = Archive()
            except Exception as exc:  # pragma: no cover - disk problems
                get_logger("crawler").warning(
                    "archive unavailable", extra={"error": str(exc), "error_code": "QFSD-2001"}
                )
                raise
        return _ARCHIVE


def reset_archive() -> None:
    global _ARCHIVE
    with _LOCK:
        _ARCHIVE = None


def item_key(source: str, row: dict[str, Any], *fallback: Any) -> str:
    """Stable archive/dedupe id for one row from ``source``."""
    return _item_id(source, row, *fallback)


__all__ = ["Archive", "SCHEMA", "get_archive", "item_key", "reset_archive"]
