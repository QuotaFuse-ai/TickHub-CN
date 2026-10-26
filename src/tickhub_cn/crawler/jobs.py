"""Offline incremental collection jobs (spec §4.12).

Each job resolves a watchlist, fetches one page window per code, deduplicates
through Redis, and writes the rows into the local SQLite archive. The archive
is the first place readers look, so a crawl is a pre-warm rather than a gate:
data is queryable as soon as it lands and an on-demand MCP call can still go
straight to the source when the archive has nothing.
"""

from __future__ import annotations

import datetime as _dt
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..cache.redis_cache import get_cache
from ..core.config import get_settings
from ..datasources.util import last_trading_day, now_shanghai
from ..logging.setup import get_logger
from ..risk.events import classify_announcement
from ..validation.financial import crosscheck, normalize_report
from . import event_publisher
from .store import get_archive, item_key

# The four T-101 tasks keep their nightly ``all`` round. T-102 market collectors
# are registered alongside them but run on their own crons, so ``all`` stays the
# announcement-oriented round it always was.
DEFAULT_TASKS = ("announcements", "reports", "risks", "northbound")
MARKET_TASKS = ("financials", "kline", "quote")
TASKS = DEFAULT_TASKS + MARKET_TASKS
DEFAULT_INDEX = "000300"
DEDUPE_TTL = 7 * 24 * 3600
PAGE_LIMIT = 50
NORTHBOUND_BACKFILL_DAYS = 60
FINANCIAL_STATEMENTS = ("balance", "income", "cashflow")
FINANCIAL_PERIODS = 8
# First phase keeps one recent window per code; the archive grows forward daily.
KLINE_LIMIT = 120
QUOTE_BATCH = 20

Progress = Callable[[dict[str, Any]], None]
StopCheck = Callable[[], bool]


@dataclass
class TaskResult:
    task: str
    status: str = "ok"
    codes: int = 0
    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    task_id: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "status": self.status,
            "codes": self.codes,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": self.errors,
            "task_id": self.task_id,
            "detail": self.detail,
        }


def _log() -> Any:
    return get_logger("crawler")


def _split_codes(raw: str) -> list[str]:
    return [c for c in re.split(r"[,;\s]+", raw or "") if c]


def resolve_watchlist(codes: Iterable[str] | str | None = None, *, limit: int | None = None) -> list[str]:
    """Explicit codes win, then the env watchlist, then index constituents."""
    settings = get_settings()
    limit = max(1, int(limit or settings.offline_crawler_max_codes or 50))
    if isinstance(codes, str):
        items = _split_codes(codes)
    elif codes:
        items = list(codes)
    else:
        items = []
    if not items:
        items = _split_codes(settings.offline_crawler_watchlist)

    if not items:
        try:
            from ..datasources import call_tool

            payload = call_tool("index_constituents", {"index_code": DEFAULT_INDEX})
            rows = ((payload or {}).get("data") or {}).get("constituents") or []
            items = [str(r.get("code")) for r in rows if r.get("code")]
            if not items:
                _log().warning(
                    "watchlist empty, index constituents unavailable",
                    extra={"event": "crawl_watchlist_empty", "error_code": "QFSD-1002"},
                )
        except Exception as exc:
            _log().warning(
                "watchlist resolution failed",
                extra={"event": "crawl_watchlist_failed", "error_code": "QFSD-1001", "error": str(exc)[:200]},
            )
            items = []

    seen: list[str] = []
    for item in items:
        code = str(item).strip()
        if code and code not in seen:
            seen.append(code)
    return seen[:limit]


def resolve_market_watchlist(codes: Iterable[str] | str | None = None, *, limit: int | None = None) -> list[str]:
    """Explicit codes, then the env watchlist — never the index.

    T-102 only covers the 自选池 in its first phase; sweeping all index
    constituents would multiply three sequential statement calls per code.
    """
    settings = get_settings()
    limit = max(1, int(limit or settings.offline_crawler_max_codes or 50))
    if isinstance(codes, str):
        items = _split_codes(codes)
    elif codes:
        items = list(codes)
    else:
        items = _split_codes(settings.offline_crawler_watchlist)
    seen: list[str] = []
    for item in items:
        code = str(item).strip()
        if code and code not in seen:
            seen.append(code)
    return seen[:limit]


def window(days: int = 7) -> tuple[str, str]:
    end = _dt.date.today()
    start = end - _dt.timedelta(days=max(1, int(days)))
    return start.isoformat(), end.isoformat()


def _rows(payload: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return [r for r in data[key] if isinstance(r, dict)]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def _call(name: str, **params: Any) -> Any:
    from ..datasources import call_tool

    return call_tool(name, params)


def _crawl_one(code: str, *, days: int, limit: int) -> tuple[list[dict[str, Any]], str]:
    start, end = window(days)
    errors: list[str] = []
    try:
        payload = _call("cninfo_announcements", code=code, start=start, end=end, limit=limit)
        rows = _rows(payload, "announcements")
        if rows:
            return rows, "cninfo"
    except Exception as exc:
        errors.append(f"cninfo: {exc}")
    try:
        payload = _call("szse_announcements", code=code, start=start, end=end, page_size=limit)
        rows = _rows(payload, "announcements")
        if rows:
            return rows, "szse"
    except Exception as exc:
        errors.append(f"szse: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors)[:300])
    return [], "cninfo"


def crawl_announcements(
    codes: Iterable[str],
    *,
    days: int = 7,
    limit: int = PAGE_LIMIT,
    concurrency: int | None = None,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
) -> TaskResult:
    settings = get_settings()
    archive = get_archive()
    cache = get_cache()
    result = TaskResult(task="announcements")
    code_list = list(codes)
    cancelled = False

    def one(code: str) -> tuple[str, list[dict[str, Any]], str]:
        return code, *_crawl_one(code, days=days, limit=limit)

    with ThreadPoolExecutor(max_workers=max(1, int(concurrency or settings.offline_crawler_concurrency or 1))) as pool:
        futures = {pool.submit(one, code): code for code in code_list}
        for future in as_completed(futures):
            # Checked before consuming each result: work already in flight is
            # bounded by the per-request timeout, pending ones are dropped.
            if should_stop is not None and should_stop():
                cancelled = True
                for pending in futures:
                    pending.cancel()
                break
            code = futures[future]
            try:
                _, rows, source = future.result()
            except Exception as exc:
                result.failed.append(code)
                result.errors[code] = str(exc)[:300]
                _log().warning(
                    "announcement crawl failed",
                    extra={"event": "crawl_code_failed", "source": "crawler", "detail": {"code": code}},
                )
                if progress:
                    progress({"task": "announcements", "code": code, "fetched": 0, "inserted": 0, "failed": True})
                continue
            result.fetched += len(rows)
            fresh = [r for r in rows if cache.dedupe(source, item_key(source, r, code, r.get("title"), r.get("publish_time")), DEDUPE_TTL)]
            result.skipped += len(rows) - len(fresh)
            inserted = archive.insert_announcements(rows, source=source)
            result.inserted += inserted
            events = []
            for row in rows:
                hit = classify_announcement(str(row.get("title") or ""))
                if not hit:
                    continue
                kind, severity, label = hit
                events.append(
                    {
                        "code": code,
                        "type": kind,
                        "label": label,
                        "severity": severity,
                        "date": row.get("publish_time") or row.get("date"),
                        "title": row.get("title"),
                        "source": source,
                    }
                )
            if events:
                archive.insert_risks(events, source="computed")
            if progress:
                progress({"task": "announcements", "code": code, "fetched": len(rows), "inserted": inserted})
    result.detail["cancelled"] = cancelled
    if cancelled:
        result.status = "cancelled"
    elif result.failed:
        result.status = "partial" if result.inserted or result.fetched else "failed"
    return result


def _crawl_reports_one(code: str, *, page_size: int) -> list[dict[str, Any]]:
    payload = _call("eastmoney_reports", code=code, page=1, page_size=page_size)
    return _rows(payload, "reports")


def crawl_reports(
    codes: Iterable[str],
    *,
    page_size: int = 30,
    concurrency: int | None = None,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
) -> TaskResult:
    settings = get_settings()
    archive = get_archive()
    cache = get_cache()
    result = TaskResult(task="reports")
    code_list = list(codes)
    cancelled = False

    with ThreadPoolExecutor(max_workers=max(1, int(concurrency or settings.offline_crawler_concurrency or 1))) as pool:
        futures = {pool.submit(_crawl_reports_one, code, page_size=page_size): code for code in code_list}
        for future in as_completed(futures):
            if should_stop is not None and should_stop():
                cancelled = True
                for pending in futures:
                    pending.cancel()
                break
            code = futures[future]
            try:
                rows = future.result()
            except Exception as exc:
                result.failed.append(code)
                result.errors[code] = str(exc)[:300]
                if progress:
                    progress({"task": "reports", "code": code, "fetched": 0, "failed": True})
                continue
            result.fetched += len(rows)
            fresh = [
                r
                for r in rows
                if cache.dedupe(
                    "eastmoney",
                    item_key("eastmoney", r, code, r.get("title"), r.get("publish_date"), r.get("org")),
                    DEDUPE_TTL,
                )
            ]
            result.skipped += len(rows) - len(fresh)
            result.inserted += archive.insert_reports(rows, source="eastmoney")
            if progress:
                progress({"task": "reports", "code": code, "fetched": len(rows), "inserted": len(fresh)})
    result.detail["cancelled"] = cancelled
    if cancelled:
        result.status = "cancelled"
    elif result.failed:
        result.status = "partial" if result.inserted or result.fetched else "failed"
    return result


def _backfill_northbound_history(archive: Any, *, days: int = NORTHBOUND_BACKFILL_DAYS) -> dict[str, Any]:
    """Write the datacenter's daily northbound turnover series into the archive.

    The 15:05 snapshot only ever owns today's row, so a fresh install shows an
    empty table until the next session. The datacenter report shares the
    ``(trade_date, channel)`` key with the snapshot, so one backfill fills the
    table immediately and a later snapshot only adds the net inflow on top.
    """
    try:
        payload = _call("northbound_flow", history=True, days=days)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300], "rows": 0, "days": 0, "inserted": 0}

    rows = _rows(payload, "days")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        day = str(row.get("trade_date") or "")
        if day:
            grouped.setdefault(day, []).append(row)
    inserted = 0
    for day, day_rows in grouped.items():
        inserted += archive.insert_northbound(day_rows, trade_date=day, source="eastmoney_datacenter")
    return {"ok": True, "rows": len(rows), "days": len(grouped), "inserted": inserted}


def crawl_northbound(*, progress: Progress | None = None, should_stop: StopCheck | None = None) -> TaskResult:
    """Snapshot the northbound/southbound flow and backfill the daily series.

    The trade date comes from weekday arithmetic, not from the calendar tool:
    the calendar itself calls push2, which is exactly what this snapshot is
    meant to spare later readers. A closed session writes no snapshot, otherwise
    a weekend run would overwrite the last trading day's real numbers. The
    datacenter backfill is independent of the session state — it is how the
    table gets real history on a fresh install and today's turnover after close.
    """
    archive = get_archive()
    result = TaskResult(task="northbound")
    if should_stop is not None and should_stop():
        result.status = "cancelled"
        result.detail["cancelled"] = True
        return result

    trade_date = last_trading_day()
    result.detail["trade_date"] = trade_date
    result.detail["cancelled"] = False

    snapshot_error = ""
    try:
        payload = _call("northbound_flow")
    except Exception as exc:
        snapshot_error = str(exc)[:300]
        payload = {}
    rows = _rows(payload, "channels")
    meta = payload.get("meta") if isinstance(payload, dict) else {}
    result.fetched = len(rows)
    if rows:
        result.inserted = archive.insert_northbound(
            rows,
            trade_date=trade_date,
            snapshot_at=now_shanghai().strftime("%Y-%m-%dT%H:%M:%S"),
        )
        result.detail["stored"] = True
    else:
        result.detail.update(
            {
                "stored": False,
                "closed": bool((meta or {}).get("closed")),
                "note": snapshot_error
                or (meta or {}).get("note")
                or "northbound returned no channel data",
            }
        )

    if should_stop is not None and should_stop():
        result.status = "cancelled"
        result.detail["cancelled"] = True
        return result

    history = _backfill_northbound_history(archive)
    result.detail["history"] = history
    result.fetched += history["rows"]
    result.inserted += history["inserted"]

    if snapshot_error:
        result.failed.append("northbound")
        result.errors["northbound"] = snapshot_error
        # The daily series still landed, so only the live sampling failed.
        result.status = "partial" if history["ok"] else "failed"
    if progress:
        progress({"task": "northbound", "fetched": result.fetched, "inserted": result.inserted})
    return result


def crawl_risks(
    codes: Iterable[str],
    *,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
) -> TaskResult:
    """Derive risk events from announcements already sitting in the archive."""
    archive = get_archive()
    result = TaskResult(task="risks")
    cancelled = False
    for code in codes:
        if should_stop is not None and should_stop():
            cancelled = True
            break
        rows = archive.announcements(code=code, limit=200)
        events = []
        for row in rows:
            hit = classify_announcement(str(row.get("title") or ""))
            if not hit:
                continue
            kind, severity, label = hit
            events.append(
                {
                    "code": code,
                    "type": kind,
                    "label": label,
                    "severity": severity,
                    "date": row.get("publish_time"),
                    "title": row.get("title"),
                    "source": row.get("source") or "computed",
                }
            )
        result.fetched += len(rows)
        result.inserted += archive.insert_risks(events, source="computed")
        if progress:
            progress({"task": "risks", "code": code, "events": len(events)})
    result.detail["cancelled"] = cancelled
    if cancelled:
        result.status = "cancelled"
    return result


def _crawl_financials_one(code: str) -> dict[str, Any]:
    """Fetch the three statements and cross-check them per report date.

    The check runs locally on rows that already arrived: ``finance_crosscheck``
    would refetch all three statements over the network per call.
    """
    statements: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for statement in FINANCIAL_STATEMENTS:
        try:
            payload = _call(
                "eastmoney_financial_report", code=code, statement=statement, periods=FINANCIAL_PERIODS
            )
            statements[statement] = _rows(payload, "reports")
        except Exception as exc:
            errors.append(f"{statement}: {exc}"[:200])
    merged: dict[str, dict[str, Any]] = {}
    for rows in statements.values():
        for row in rows:
            day = str(row.get("report_date") or "")[:10]
            if day:
                merged.setdefault(day, {}).update(normalize_report(row))
    checks = {day: crosscheck(values) for day, values in merged.items() if values}
    return {"statements": statements, "checks": checks, "errors": errors}


def crawl_financials(
    codes: Iterable[str],
    *,
    concurrency: int | None = None,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
) -> TaskResult:
    settings = get_settings()
    archive = get_archive()
    result = TaskResult(task="financials")
    code_list = list(codes)
    cancelled = False

    with ThreadPoolExecutor(max_workers=max(1, int(concurrency or settings.offline_crawler_concurrency or 1))) as pool:
        futures = {pool.submit(_crawl_financials_one, code): code for code in code_list}
        for future in as_completed(futures):
            if should_stop is not None and should_stop():
                cancelled = True
                for pending in futures:
                    pending.cancel()
                break
            code = futures[future]
            try:
                data = future.result()
            except Exception as exc:
                result.failed.append(code)
                result.errors[code] = str(exc)[:300]
                if progress:
                    progress({"task": "financials", "code": code, "fetched": 0, "failed": True})
                continue
            inserted = 0
            for statement, rows in data["statements"].items():
                result.fetched += len(rows)
                inserted += archive.insert_financial_reports(code, statement, rows, checks=data["checks"])
            result.inserted += inserted
            if data["errors"]:
                result.failed.append(code)
                result.errors[code] = "; ".join(data["errors"])
            if progress:
                progress({"task": "financials", "code": code, "fetched": sum(len(r) for r in data["statements"].values()), "inserted": inserted})
    result.detail["cancelled"] = cancelled
    if cancelled:
        result.status = "cancelled"
    elif result.failed:
        result.status = "partial" if result.inserted or result.fetched else "failed"
    return result


def _crawl_kline_one(code: str) -> list[dict[str, Any]]:
    payload = _call("tencent_kline", code=code, period="daily", adjust="qfq", limit=KLINE_LIMIT)
    return _rows(payload, "klines")


def crawl_kline(
    codes: Iterable[str],
    *,
    concurrency: int | None = None,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
) -> TaskResult:
    settings = get_settings()
    archive = get_archive()
    result = TaskResult(task="kline")
    code_list = list(codes)
    cancelled = False

    with ThreadPoolExecutor(max_workers=max(1, int(concurrency or settings.offline_crawler_concurrency or 1))) as pool:
        futures = {pool.submit(_crawl_kline_one, code): code for code in code_list}
        for future in as_completed(futures):
            if should_stop is not None and should_stop():
                cancelled = True
                for pending in futures:
                    pending.cancel()
                break
            code = futures[future]
            try:
                rows = future.result()
            except Exception as exc:
                result.failed.append(code)
                result.errors[code] = str(exc)[:300]
                if progress:
                    progress({"task": "kline", "code": code, "fetched": 0, "failed": True})
                continue
            result.fetched += len(rows)
            inserted = archive.insert_klines(code, rows, period="day", adjust="qfq")
            result.inserted += inserted
            if progress:
                progress({"task": "kline", "code": code, "fetched": len(rows), "inserted": inserted})
    result.detail["cancelled"] = cancelled
    if cancelled:
        result.status = "cancelled"
    elif result.failed:
        result.status = "partial" if result.inserted or result.fetched else "failed"
    return result


def _quote_rows(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return [], []
    quotes = data.get("quotes")
    rows = [r for r in quotes.values() if isinstance(r, dict)] if isinstance(quotes, dict) else []
    failures = data.get("failures")
    return rows, [f for f in failures if isinstance(f, dict)] if isinstance(failures, list) else []


def crawl_quote(
    codes: Iterable[str],
    *,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
) -> TaskResult:
    """Close snapshot for PE/PB (G-05); one archive row per code and trade date."""
    archive = get_archive()
    result = TaskResult(task="quote")
    code_list = list(codes)
    trade_date = last_trading_day()
    result.detail["trade_date"] = trade_date
    cancelled = False

    for start in range(0, len(code_list), QUOTE_BATCH):
        if should_stop is not None and should_stop():
            cancelled = True
            break
        chunk = code_list[start : start + QUOTE_BATCH]
        try:
            payload = _call("tencent_quote_batch", codes=",".join(chunk), max_codes=len(chunk))
            rows, failures = _quote_rows(payload)
        except Exception as exc:
            for code in chunk:
                result.failed.append(code)
                result.errors[code] = str(exc)[:300]
            if progress:
                progress({"task": "quote", "codes": len(chunk), "fetched": 0, "failed": True})
            continue
        result.fetched += len(rows)
        inserted = archive.insert_quotes(rows, trade_date=trade_date)
        result.inserted += inserted
        for failure in failures:
            code = str(failure.get("code") or "")
            if code:
                result.failed.append(code)
                result.errors[code] = str(failure.get("error") or "")[:300]
        if progress:
            progress({"task": "quote", "codes": len(chunk), "fetched": len(rows), "inserted": inserted})
    result.detail["cancelled"] = cancelled
    if cancelled:
        result.status = "cancelled"
    elif result.failed:
        result.status = "partial" if result.inserted or result.fetched else "failed"
    return result


_HEARTBEAT_STATUS = {
    "ok": "ok",
    "empty": "ok",
    "partial": "partial",
    "cancelled": "partial",
    "failed": "failed",
}


def _round_end(*, task: str, status: str, inserted: int, started: _dt.datetime) -> None:
    """Heartbeat and outbox flush after a round (success, partial or failure)."""
    try:
        event_publisher.heartbeat(
            provider="tickhub-cn",
            market="CN",
            run_id=f"cn-{started:%Y%m%d}-{task}",
            status=_HEARTBEAT_STATUS.get(status, "failed"),
            items=inserted,
            started_at=started,
            finished_at=now_shanghai(),
        )
    except Exception as exc:  # the side channel must never fail a crawl
        _log().warning(
            "crawl heartbeat failed",
            extra={"event": "event_heartbeat_failed", "error_code": "QFSD-1001", "error": str(exc)[:200]},
        )
    try:
        event_publisher.flush_outbox()
    except Exception as exc:
        _log().warning(
            "event outbox flush failed",
            extra={"event": "event_outbox_flush_failed", "error_code": "QFSD-1001", "error": str(exc)[:200]},
        )


def run_task(
    task: str = "all",
    *,
    codes: Iterable[str] | str | None = None,
    days: int = 7,
    limit: int | None = None,
    concurrency: int | None = None,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
) -> dict[str, Any]:
    """Run one crawl task.

    ``all`` keeps the announcement-oriented round (announcements / reports /
    risks / northbound); ``financials`` / ``kline`` / ``quote`` run on their own
    crons. ``should_stop`` is polled between codes and between sub-tasks. A
    cancelled run keeps whatever already landed in the archive, skips the
    remaining sub-tasks, and reports ``status="cancelled"`` with
    ``detail["cancelled"]``.
    """
    name = (task or "all").strip().lower()
    if name != "all" and name not in TASKS:
        raise ValueError(f"unknown crawl task: {task} (expected one of {', '.join(TASKS + ('all',))})")

    settings = get_settings()
    market_only = name in MARKET_TASKS
    if market_only:
        watchlist = resolve_market_watchlist(codes, limit=limit or settings.offline_crawler_max_codes)
    else:
        watchlist = resolve_watchlist(codes, limit=limit or settings.offline_crawler_max_codes)
    archive = get_archive()
    task_id = archive.start_task(name, {"codes": len(watchlist), "days": days})
    started = now_shanghai()
    plan = list(DEFAULT_TASKS) if name == "all" else [name]
    if progress:
        progress({"phase": "start", "task": name, "codes": len(watchlist), "tasks": plan})

    results: list[TaskResult] = []
    cancelled = False

    def stop() -> bool:
        return should_stop is not None and should_stop()

    try:
        if "announcements" in plan and not stop():
            results.append(
                crawl_announcements(
                    watchlist,
                    days=days,
                    limit=limit or PAGE_LIMIT,
                    concurrency=concurrency,
                    progress=progress,
                    should_stop=should_stop,
                )
            )
            cancelled = results[-1].status == "cancelled"
        if "reports" in plan and not cancelled and not stop():
            results.append(
                crawl_reports(watchlist, concurrency=concurrency, progress=progress, should_stop=should_stop)
            )
            cancelled = results[-1].status == "cancelled"
        if "risks" in plan and not cancelled and not stop():
            results.append(crawl_risks(watchlist, progress=progress, should_stop=should_stop))
            cancelled = results[-1].status == "cancelled"
        if "northbound" in plan and not cancelled and not stop():
            results.append(crawl_northbound(progress=progress, should_stop=should_stop))
            cancelled = results[-1].status == "cancelled"
        if "financials" in plan and not cancelled and not stop():
            results.append(
                crawl_financials(watchlist, concurrency=concurrency, progress=progress, should_stop=should_stop)
            )
            cancelled = results[-1].status == "cancelled"
        if "kline" in plan and not cancelled and not stop():
            results.append(
                crawl_kline(watchlist, concurrency=concurrency, progress=progress, should_stop=should_stop)
            )
            cancelled = results[-1].status == "cancelled"
        if "quote" in plan and not cancelled and not stop():
            results.append(crawl_quote(watchlist, progress=progress, should_stop=should_stop))
            cancelled = results[-1].status == "cancelled"
    except Exception as exc:
        elapsed = (now_shanghai() - started).total_seconds()
        archive.finish_task(task_id, status="failed", error=str(exc), detail={"elapsed_s": round(elapsed, 1)})
        _log().error(
            "crawl task crashed",
            extra={"event": "crawl_task_failed", "error_code": "QFSD-1001", "detail": {"task": name}},
        )
        _round_end(task=name, status="failed", inserted=0, started=started)
        raise

    fetched = sum(r.fetched for r in results)
    inserted = sum(r.inserted for r in results)
    failed = sorted({code for r in results for code in r.failed})
    cancelled = cancelled or stop()
    if cancelled:
        status = "cancelled"
    elif not watchlist and not fetched:
        status = "empty"
    elif failed:
        status = "partial" if fetched or inserted else "failed"
    else:
        status = "ok"
    elapsed = (now_shanghai() - started).total_seconds()
    detail = {
        "codes": len(watchlist),
        "elapsed_s": round(elapsed, 1),
        "cancelled": cancelled,
        "tasks": [r.as_dict() for r in results],
    }
    if market_only and not watchlist:
        detail["note"] = "watchlist empty; set OFFLINE_CRAWLER_WATCHLIST"
    archive.finish_task(
        task_id,
        status=status,
        fetched=fetched,
        inserted=inserted,
        error="; ".join(failed)[:500],
        detail=detail,
    )
    _log().info(
        "crawl task finished",
        extra={
            "event": "crawl_task_done",
            "elapsed_ms": int(elapsed * 1000),
            "detail": {"task": name, "status": status, "codes": len(watchlist), "fetched": fetched, "inserted": inserted},
        },
    )
    _round_end(task=name, status=status, inserted=inserted, started=started)
    return {
        "task": name,
        "task_id": task_id,
        "status": status,
        "codes": len(watchlist),
        "fetched": fetched,
        "inserted": inserted,
        "failed": failed,
        "cancelled": cancelled,
        "elapsed_s": round(elapsed, 1),
        "tasks": [r.as_dict() for r in results],
    }


def crawl_status() -> dict[str, Any]:
    settings = get_settings()
    archive = get_archive()
    cache = get_cache()
    try:
        stats = archive.stats()
    except Exception as exc:
        stats = {"error": str(exc)[:200]}
    configured = [c for c in settings.offline_crawler_watchlist.replace(",", " ").split() if c]
    return {
        "enabled": settings.offline_crawler_enable,
        "cron": settings.offline_crawler_cron,
        "northbound_cron": settings.offline_crawler_northbound_cron,
        "financials_cron": settings.offline_crawler_financials_cron,
        "financials_season_cron": settings.offline_crawler_financials_season_cron,
        "kline_cron": settings.offline_crawler_kline_cron,
        "quote_cron": settings.offline_crawler_quote_cron,
        "concurrency": settings.offline_crawler_concurrency,
        "watchlist": len(configured) or f"index:{DEFAULT_INDEX}",
        "market_watchlist": len(configured),
        "max_codes": settings.offline_crawler_max_codes,
        "archive": stats,
        "queue": {task: cache.queue_length(task) for task in TASKS},
        "events": event_publisher.stats(),
        "last_task": archive.last_task(),
    }


def archive_announcements(code: str = "", limit: int = 50, *, refresh: bool = False) -> list[dict[str, Any]]:
    """Archive-first read; ``refresh`` pulls a fresh window for that code."""
    archive = get_archive()
    rows = archive.announcements(code=code, limit=limit)
    if rows or not code or not refresh:
        return rows
    from ..datasources import call_tool

    start, end = window(7)
    payload = call_tool("cninfo_announcements", {"code": code, "start": start, "end": end, "limit": limit})
    fresh = _rows(payload, "announcements")
    archive.insert_announcements(fresh, source="cninfo")
    return archive.announcements(code=code, limit=limit)


__all__ = [
    "DEFAULT_INDEX",
    "DEFAULT_TASKS",
    "MARKET_TASKS",
    "TASKS",
    "TaskResult",
    "archive_announcements",
    "crawl_announcements",
    "crawl_financials",
    "crawl_kline",
    "crawl_northbound",
    "crawl_quote",
    "crawl_reports",
    "crawl_risks",
    "crawl_status",
    "resolve_market_watchlist",
    "resolve_watchlist",
    "run_task",
    "window",
]
