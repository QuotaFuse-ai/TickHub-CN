"""Local archive backing the offline crawler.

SQLite by default (zero setup, ships with Python). Tables the crawler does not
fill yet are still created so operators and downstream jobs see a stable shape.
Writes are idempotent so a re-run never duplicates an announcement or a report.
"""

from __future__ import annotations

import datetime as _dt
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
from . import event_publisher

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
CREATE TABLE IF NOT EXISTS quote_snapshot (
    code TEXT, trade_date TEXT, close REAL, pe REAL, pb REAL,
    total_market_cap REAL, float_market_cap REAL, payload TEXT, crawl_time TEXT,
    PRIMARY KEY (code, trade_date)
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


# ------------------------------------------------------------------ events
#
# Archive columns keep the raw vendor values; the event envelope needs business
# time and string amounts. Chinese sources publish in Asia/Shanghai, while a
# date-only value ("2026-10-23") means the trading/business day itself, so it is
# pinned to UTC midnight to keep that calendar date intact downstream.

_CN_TZ = _dt.timezone(_dt.timedelta(hours=8))


def _business_date(value: Any) -> str:
    text = str(value or "").strip().replace("/", "-")
    if not text:
        return ""
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        stamp = float(text)
    except ValueError:
        return ""
    return _dt.datetime.fromtimestamp(stamp / 1000, _CN_TZ).strftime("%Y-%m-%d")


def _business_time(value: Any) -> _dt.datetime:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    parsed: _dt.datetime | None = None
    if text:
        try:
            parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00").replace("/", "-"))
        except ValueError:
            try:
                parsed = _dt.datetime.fromtimestamp(float(text) / 1000, _CN_TZ)
            except ValueError:
                parsed = None
    if parsed is None:
        return _dt.datetime.now(_dt.timezone.utc)
    if parsed.tzinfo is not None:
        return parsed
    # A bare date is a business day, not an instant; a time-of-day is local.
    if parsed.hour or parsed.minute or parsed.second:
        return parsed.replace(tzinfo=_CN_TZ)
    return parsed.replace(tzinfo=_dt.timezone.utc)


def _amount(value: Any) -> str | None:
    """Contract rule: amounts travel as strings to avoid float drift."""
    if value is None or value == "":
        return None
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def _existing_ids(con: sqlite3.Connection, table: str, ids: list[str]) -> set[str]:
    found: set[str] = set()
    unique = list(dict.fromkeys(ids))
    for start in range(0, len(unique), event_publisher.IN_CHUNK):
        chunk = unique[start : start + event_publisher.IN_CHUNK]
        marks = ",".join("?" for _ in chunk)
        found.update(row[0] for row in con.execute(f"SELECT item_id FROM {table} WHERE item_id IN ({marks})", chunk))
    return found


def _log_event_error(exc: Exception) -> None:
    get_logger("crawler").warning(
        "event build failed",
        extra={"event": "event_build_failed", "error_code": "QFSD-1001", "error": str(exc)[:200]},
    )


# DEC-0006 / DEC-0009: CN sources are personal-research level (tier C) and the
# same non-commercial declaration as US data applies, so every CN data event
# carries both fields. Unknown/new sources default to C (fail-closed).
CN_COMPLIANCE_TIER = "C"


def _source_info(provider: str, **extra: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "provider": provider,
        "compliance_tier": CN_COMPLIANCE_TIER,
        "commercial_use": False,
    }
    info.update({name: value for name, value in extra.items() if value is not None and value != ""})
    return info


def _announcement_fields(row: dict[str, Any]) -> tuple[str, str, str, Any]:
    code = str(row.get("code") or row.get("stockCode") or "")
    title = str(row.get("title") or row.get("announcementTitle") or "")
    published = str(
        row.get("publish_time") or row.get("date") or row.get("announcementTime") or row.get("NOTICE_DATE") or ""
    )
    # Kept raw: a missing URL stays NULL in the archive, as it always did.
    url = row.get("url") or row.get("adjunctUrl") or row.get("pdf")
    return code, title, published, url


def _announcement_event(
    source: str, item_id: str, row: dict[str, Any], *, code: str, title: str, published: str, url: Any
) -> dict[str, Any]:
    ann_type = str(row.get("type") or row.get("announcementTypeName") or "")
    fields: dict[str, Any] = {"ann_type": ann_type, "publish_date": _business_date(published)}
    if title:
        fields["title"] = title
    return event_publisher.build_event(
        provider=source,
        data_type="announcement",
        symbol=code,
        market="CN",
        biz_key=item_id,
        event_time=_business_time(published),
        content={
            "clean_content": title,
            "structured_fields": fields,
            "quality_metrics": {"confidence": "high", "quality_status": "normal"},
        },
        source_info=_source_info(source, crawl_url=url),
    )


def _report_fields(row: dict[str, Any]) -> tuple[str, str, str]:
    code = str(row.get("code") or "")
    title = str(row.get("title") or "")
    published = str(row.get("publish_date") or row.get("publishTime") or "")
    return code, title, published


def _report_key(row: dict[str, Any], *, title: str, published: str) -> str:
    ident = str(row.get("info_code") or row.get("infoCode") or row.get("report_id") or "")
    if ident:
        return ident
    return f"{title}|{row.get('org') or row.get('institution') or ''}|{published}"


def _report_event(
    source: str, row: dict[str, Any], *, code: str, title: str, published: str, biz_key: str
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    ident = str(row.get("info_code") or row.get("infoCode") or row.get("report_id") or "")
    if ident:
        fields["report_id"] = ident
    if title:
        fields["title"] = title
    for column, name in (("org", "institution"), ("analyst", "analyst"), ("rating", "rating")):
        value = row.get(column) or row.get(name)
        if value:
            fields[name] = str(value)
    target = row.get("target_price")
    if target is not None:
        fields["target_price"] = _amount(target)
    publish_date = _business_date(published)
    if publish_date:
        fields["publish_date"] = publish_date
    pdf = row.get("pdf") or row.get("url")
    if pdf:
        fields["pdf_url"] = str(pdf)
    return event_publisher.build_event(
        provider=source,
        data_type="research",
        symbol=code,
        market="CN",
        biz_key=biz_key,
        event_time=_business_time(published),
        content={
            "structured_fields": fields,
            "quality_metrics": {"confidence": "medium", "quality_status": "normal"},
        },
        source_info=_source_info(source, crawl_url=str(pdf or "")),
    )


def _risk_fields(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("code") or ""),
        str(row.get("type") or ""),
        str(row.get("date") or ""),
        str(row.get("title") or ""),
    )


def _risk_event(
    fallback_source: str, item_id: str, row: dict[str, Any], *, code: str, risk_type: str, occurred: str, title: str
) -> dict[str, Any]:
    provider = str(row.get("source") or fallback_source)
    fields: dict[str, Any] = {
        "risk_type": risk_type,
        "occur_date": _business_date(occurred),
        "summary": title,
    }
    if row.get("severity"):
        fields["severity"] = str(row.get("severity"))
    if row.get("label"):
        fields["label"] = str(row.get("label"))
    return event_publisher.build_event(
        provider=provider,
        data_type="risk",
        symbol=code,
        market="CN",
        biz_key=f"{fields['occur_date']}|{risk_type}|{item_id}",
        event_time=_business_time(occurred),
        content={
            "structured_fields": fields,
            "quality_metrics": {"confidence": "medium", "quality_status": "normal"},
        },
        source_info=_source_info(provider),
    )


def _northbound_event(day: str, channel: str, row: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "trade_date": day,
        "channel": str(row.get("name") or channel),
        "currency": "CNY",
    }
    net = _amount(row.get("net_inflow"))
    if net is not None:
        fields["net_buy_amount"] = net
    turnover = _amount(row.get("deal_amount_wan"))
    if turnover is not None:
        fields["deal_amount_wan"] = turnover
    if row.get("deal_num") is not None:
        try:
            fields["deal_num"] = int(row["deal_num"])
        except (TypeError, ValueError):
            pass
    if row.get("quota_text"):
        fields["quota_text"] = str(row.get("quota_text"))
    return event_publisher.build_event(
        # Fixed provider: the snapshot and the history backfill describe the same
        # (trade_date, channel) fact, so both must hash to one event_id.
        provider="eastmoney_datacenter",
        data_type="northbound",
        symbol=None,
        market="CN",
        biz_key=f"{day}|{channel}",
        event_time=_business_time(day),
        content={
            "structured_fields": fields,
            "quality_metrics": {
                "confidence": "high" if net is not None else "medium",
                "quality_status": "normal",
            },
        },
        source_info=_source_info("eastmoney_datacenter"),
    )


# ------------------------------------------------------- T-102 market events
#
# Financials, K-line and close quotes only became archive writers in T-102.
# Their event ids follow the same provider|data_type|symbol|biz_key recipe as
# the four original types, so a re-run that inserts nothing publishes nothing.

_REPORT_TYPES = {3: "Q1", 6: "H1", 9: "Q3", 12: "Annual"}
_CHECK_QUALITY = {"pass": "normal", "warn": "suspicious", "fail": "bad", "skip": "suspicious"}


def _report_type(report_date: str) -> str:
    try:
        return _REPORT_TYPES.get(int(report_date[5:7]), "")
    except (TypeError, ValueError):
        return ""


def _financial_event(
    statement: str, row: dict[str, Any], *, code: str, report_date: str, check: dict[str, Any] | None
) -> dict[str, Any]:
    fields: dict[str, Any] = {"report_date": report_date, "statement": statement}
    report_type = _report_type(report_date)
    if report_type:
        fields["report_type"] = report_type
    revenue = _amount(row.get("revenue"))
    if revenue is not None:
        fields["amount_revenue"] = revenue
    profit = row.get("net_profit")
    if profit is None:
        profit = row.get("parent_net_profit")
    amount = _amount(profit)
    if amount is not None:
        fields["amount_net_profit"] = amount
    check = check or {}
    status = str(check.get("status") or "skip")
    metrics: dict[str, Any] = {
        "confidence": "high",
        "quality_status": _CHECK_QUALITY.get(status, "suspicious"),
    }
    if check:
        metrics["balance_check_result"] = check
    return event_publisher.build_event(
        provider="eastmoney",
        data_type="financial",
        symbol=code,
        market="CN",
        biz_key=f"{report_date}|{statement}",
        event_time=report_date,
        content={"structured_fields": fields, "quality_metrics": metrics},
        source_info={"provider": "eastmoney"},
    )


def _kline_fields(row: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "trade_date": str(row.get("trade_date") or row.get("date") or "")[:10],
    }
    for name in ("open", "high", "low", "close", "amount"):
        value = _amount(row.get(name))
        if value is not None:
            fields[name] = value
    volume = row.get("volume")
    if volume is not None:
        try:
            fields["volume"] = str(int(float(volume)))
        except (TypeError, ValueError):
            fields["volume"] = str(volume)
    return fields


def _kline_event(code: str, row: dict[str, Any], *, period: str, adjust: str) -> dict[str, Any]:
    fields = _kline_fields(row)
    fields["period"] = period
    fields["adjust"] = adjust
    return event_publisher.build_event(
        provider="tencent",
        data_type="kline",
        symbol=code,
        market="CN",
        biz_key=f"{fields['trade_date']}|{period}|{adjust}",
        event_time=fields["trade_date"],
        content={
            "structured_fields": fields,
            "quality_metrics": {"confidence": "high", "quality_status": "normal"},
        },
        source_info={"provider": "tencent"},
    )


def _tencent_time(value: Any) -> _dt.datetime | None:
    """Tencent stamps quotes as ``20261027150003`` (local) or a slashed time."""
    text = str(value or "").strip()
    for fmt in ("%Y%m%d%H%M%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(text, fmt).replace(tzinfo=_CN_TZ)
        except ValueError:
            continue
    return None


def _quote_event(
    code: str, row: dict[str, Any], *, trade_date: str, stamp: _dt.datetime | None
) -> dict[str, Any]:
    day = stamp.strftime("%Y-%m-%d") if stamp else trade_date
    biz = f"{day}|{stamp.strftime('%H%M%S') if stamp else day}"
    fields: dict[str, Any] = {"trade_date": day}
    close = _amount(row.get("price") if row.get("price") is not None else row.get("close"))
    if close is not None:
        fields["close"] = close
    for key, name in (("total_market_cap", "total_market_cap"), ("pe", "pe_ttm"), ("pb", "pb")):
        value = _amount(row.get(key))
        if value is not None:
            fields[name] = value
    return event_publisher.build_event(
        provider="tencent",
        data_type="quote",
        symbol=code,
        market="CN",
        biz_key=biz,
        event_time=stamp or day,
        content={
            "structured_fields": fields,
            "quality_metrics": {
                "confidence": "high",
                "quality_status": "suspicious" if row.get("is_stale") else "normal",
            },
        },
        source_info={"provider": "tencent"},
    )


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
        records: list[tuple[str, dict[str, Any], str, str, str, Any]] = []
        seen: set[str] = set()
        for row in rows or []:
            code, title, published, url = _announcement_fields(row)
            item_id = _item_id(source, row, code, title, published)
            if item_id in seen:
                continue
            seen.add(item_id)
            records.append((item_id, row, code, title, published, url))
        publishing = bool(records) and event_publisher.is_enabled()
        fresh: list[tuple[str, dict[str, Any], str, str, str, Any]] = []
        with self._lock, self.connect() as con:
            existing = _existing_ids(con, "announcement", [r[0] for r in records]) if publishing else set()
            for item_id, row, code, title, published, url in records:
                cur = con.execute(
                    "INSERT OR IGNORE INTO announcement"
                    " (item_id, code, title, publish_time, url, source, crawl_time, payload)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (item_id, code, title, published, url, source, stamp, json.dumps(row, ensure_ascii=False, default=str)),
                )
                if not cur.rowcount:
                    continue
                inserted += 1
                if publishing and item_id not in existing:
                    fresh.append((item_id, row, code, title, published, url))
        for item_id, row, code, title, published, url in fresh:
            try:
                event_publisher.publish(
                    _announcement_event(
                        source, item_id, row, code=code, title=title, published=published, url=url
                    )
                )
            except Exception as exc:  # publishing is a side path, never a crawl failure
                _log_event_error(exc)
        return inserted

    def insert_reports(self, rows: list[dict[str, Any]], *, source: str = "eastmoney") -> int:
        inserted = 0
        stamp = _now()
        records: list[tuple[str, dict[str, Any], str, str, str, str]] = []
        seen: set[str] = set()
        for row in rows or []:
            code, title, published = _report_fields(row)
            item_id = _item_id(source, row, code, title, published, row.get("org"))
            if item_id in seen:
                continue
            seen.add(item_id)
            records.append((item_id, row, code, title, published, _report_key(row, title=title, published=published)))
        publishing = bool(records) and event_publisher.is_enabled()
        fresh: list[tuple[str, dict[str, Any], str, str, str, str]] = []
        with self._lock, self.connect() as con:
            existing = _existing_ids(con, "research_report", [r[0] for r in records]) if publishing else set()
            for item_id, row, code, title, published, biz_key in records:
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
                if not cur.rowcount:
                    continue
                inserted += 1
                if publishing and item_id not in existing:
                    fresh.append((item_id, row, code, title, published, biz_key))
        for item_id, row, code, title, published, biz_key in fresh:
            try:
                event_publisher.publish(
                    _report_event(source, row, code=code, title=title, published=published, biz_key=biz_key)
                )
            except Exception as exc:
                _log_event_error(exc)
        return inserted

    def insert_risks(self, rows: list[dict[str, Any]], *, source: str = "computed") -> int:
        inserted = 0
        stamp = _now()
        records: list[tuple[str, dict[str, Any], str, str, str, str]] = []
        seen: set[str] = set()
        for row in rows or []:
            code, risk_type, occurred, title = _risk_fields(row)
            item_id = _item_id(source, row, row.get("code"), row.get("type"), row.get("title"), row.get("date"))
            if item_id in seen:
                continue
            seen.add(item_id)
            records.append((item_id, row, code, risk_type, occurred, title))
        publishing = bool(records) and event_publisher.is_enabled()
        fresh: list[tuple[str, dict[str, Any], str, str, str, str]] = []
        with self._lock, self.connect() as con:
            existing = _existing_ids(con, "risk_event", [r[0] for r in records]) if publishing else set()
            for item_id, row, code, risk_type, occurred, title in records:
                cur = con.execute(
                    "INSERT OR IGNORE INTO risk_event"
                    " (item_id, code, type, severity, event_date, title, source, crawl_time, payload)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        item_id,
                        code,
                        risk_type,
                        str(row.get("severity") or ""),
                        occurred,
                        title,
                        str(row.get("source") or source),
                        stamp,
                        json.dumps(row, ensure_ascii=False, default=str),
                    ),
                )
                if not cur.rowcount:
                    continue
                inserted += 1
                if publishing and item_id not in existing:
                    fresh.append((item_id, row, code, risk_type, occurred, title))
        for item_id, row, code, risk_type, occurred, title in fresh:
            try:
                event_publisher.publish(
                    _risk_event(
                        source, item_id, row, code=code, risk_type=risk_type, occurred=occurred, title=title
                    )
                )
            except Exception as exc:
                _log_event_error(exc)
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
        publishing = bool(rows) and event_publisher.is_enabled()
        fresh: list[tuple[str, str, dict[str, Any]]] = []
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
                if existing is None and publishing:
                    fresh.append((day, channel, merged))
        for day, channel, merged in fresh:
            try:
                event_publisher.publish(_northbound_event(day, channel, merged))
            except Exception as exc:
                _log_event_error(exc)
        return inserted

    def insert_financial_reports(
        self,
        code: str,
        statement: str,
        rows: list[dict[str, Any]],
        *,
        checks: dict[str, dict[str, Any]] | None = None,
        source: str = "eastmoney",
    ) -> int:
        """Archive one statement; ``checks`` maps report_date → crosscheck result."""
        inserted = 0
        stamp = _now()
        checks = checks or {}
        records: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for row in rows or []:
            report_date = str(row.get("report_date") or row.get("reportdate") or "")[:10]
            if not report_date or report_date in seen:
                continue
            seen.add(report_date)
            records.append((report_date, row))
        publishing = bool(records) and event_publisher.is_enabled()
        fresh: list[tuple[str, dict[str, Any]]] = []
        with self._lock, self.connect() as con:
            existing: set[str] = set()
            if publishing:
                marks = ",".join("?" for _ in records)
                existing = {
                    r[0]
                    for r in con.execute(
                        f"SELECT report_date FROM financial_report WHERE code=? AND statement=?"
                        f" AND report_date IN ({marks})",
                        [code, statement, *(r[0] for r in records)],
                    )
                }
            for report_date, row in records:
                cur = con.execute(
                    "INSERT OR IGNORE INTO financial_report (code, report_date, statement, payload, crawl_time)"
                    " VALUES (?,?,?,?,?)",
                    (code, report_date, statement, json.dumps(row, ensure_ascii=False, default=str), stamp),
                )
                if not cur.rowcount:
                    continue
                inserted += 1
                if publishing and report_date not in existing:
                    fresh.append((report_date, row))
        for report_date, row in fresh:
            try:
                event_publisher.publish(
                    _financial_event(
                        statement, row, code=code, report_date=report_date, check=checks.get(report_date)
                    )
                )
            except Exception as exc:
                _log_event_error(exc)
        return inserted

    def insert_klines(
        self,
        code: str,
        rows: list[dict[str, Any]],
        *,
        period: str = "day",
        adjust: str = "qfq",
        source: str = "tencent",
    ) -> int:
        inserted = 0
        stamp = _now()
        records: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for row in rows or []:
            trade_date = str(row.get("trade_date") or row.get("date") or "")[:10]
            if not trade_date or trade_date in seen:
                continue
            seen.add(trade_date)
            records.append((trade_date, row))
        publishing = bool(records) and event_publisher.is_enabled()
        fresh: list[tuple[str, dict[str, Any]]] = []
        with self._lock, self.connect() as con:
            existing: set[str] = set()
            if publishing:
                marks = ",".join("?" for _ in records)
                existing = {
                    r[0]
                    for r in con.execute(
                        f"SELECT trade_date FROM kline WHERE code=? AND period=? AND adjust=?"
                        f" AND trade_date IN ({marks})",
                        [code, period, adjust, *(r[0] for r in records)],
                    )
                }
            for trade_date, row in records:
                cur = con.execute(
                    "INSERT OR IGNORE INTO kline"
                    " (code, period, adjust, trade_date, open, high, low, close, volume, amount, payload, crawl_time)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        code,
                        period,
                        adjust,
                        trade_date,
                        row.get("open"),
                        row.get("high"),
                        row.get("low"),
                        row.get("close"),
                        row.get("volume"),
                        row.get("amount"),
                        json.dumps(row, ensure_ascii=False, default=str),
                        stamp,
                    ),
                )
                if not cur.rowcount:
                    continue
                inserted += 1
                if publishing and trade_date not in existing:
                    fresh.append((trade_date, row))
        for trade_date, row in fresh:
            try:
                event_publisher.publish(_kline_event(code, row, period=period, adjust=adjust))
            except Exception as exc:
                _log_event_error(exc)
        return inserted

    def insert_quotes(
        self, rows: list[dict[str, Any]], *, trade_date: str = "", source: str = "tencent"
    ) -> int:
        """One close snapshot per (code, trade_date); reruns add nor publish nothing."""
        inserted = 0
        stamp = _now()
        publishing = bool(rows) and event_publisher.is_enabled()
        fresh: list[tuple[str, dict[str, Any], str, _dt.datetime | None]] = []
        seen: set[tuple[str, str]] = set()
        with self._lock, self.connect() as con:
            for row in rows or []:
                code = str(row.get("code") or "")
                stamp_time = _tencent_time(row.get("timestamp"))
                day = stamp_time.strftime("%Y-%m-%d") if stamp_time else (trade_date or _now()[:10])
                if not code or not day or (code, day) in seen:
                    continue
                seen.add((code, day))
                existing = con.execute(
                    "SELECT 1 FROM quote_snapshot WHERE code=? AND trade_date=?", (code, day)
                ).fetchone()
                cur = con.execute(
                    "INSERT OR IGNORE INTO quote_snapshot"
                    " (code, trade_date, close, pe, pb, total_market_cap, float_market_cap, payload, crawl_time)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        code,
                        day,
                        row.get("price"),
                        row.get("pe"),
                        row.get("pb"),
                        row.get("total_market_cap"),
                        row.get("float_market_cap"),
                        json.dumps(row, ensure_ascii=False, default=str),
                        stamp,
                    ),
                )
                if not cur.rowcount:
                    continue
                inserted += 1
                if publishing and existing is None:
                    fresh.append((code, row, day, stamp_time))
        for code, row, day, stamp_time in fresh:
            try:
                event_publisher.publish(_quote_event(code, row, trade_date=day, stamp=stamp_time))
            except Exception as exc:
                _log_event_error(exc)
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
        allowed = {
            "announcement",
            "research_report",
            "risk_event",
            "crawl_task",
            "northbound_flow",
            "financial_report",
            "kline",
            "quote_snapshot",
        }
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

    def financial_reports(self, code: str = "", limit: int = 50) -> list[dict[str, Any]]:
        return self.query("financial_report", code=code, limit=limit, order="report_date")

    def klines(self, code: str = "", limit: int = 200) -> list[dict[str, Any]]:
        return self.query("kline", code=code, limit=limit, order="trade_date")

    def quotes(self, code: str = "", limit: int = 200) -> list[dict[str, Any]]:
        return self.query("quote_snapshot", code=code, limit=limit, order="trade_date")

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
        tables = (
            "announcement",
            "research_report",
            "risk_event",
            "northbound_flow",
            "financial_report",
            "kline",
            "quote_snapshot",
            "crawl_task",
        )
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
