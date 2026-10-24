"""Response envelope with provenance metadata."""

from __future__ import annotations

import datetime as _dt
import uuid
from contextvars import ContextVar
from typing import Any

_TRACE: ContextVar[str | None] = ContextVar("tickhub_trace_id", default=None)

CONFIDENCE = {
    "exchange": "A",
    "tencent": "A",
    "sina": "B",
    "cninfo": "A",
    "sse": "A",
    "szse": "A",
    "bse": "A",
    "eastmoney": "B",
    "ths": "C",
    "baidu": "C",
    "baostock": "B",
    "mootdx": "B",
    "iwencai": "C",
    "cache": "C",
    "computed": "C",
    "wallstreetcn": "C",
    "cls": "C",
    "cctv": "A",
    "pbc": "A",
    "stats": "A",
    "chinabond": "A",
    "chinamoney": "A",
    "csindex": "A",
    "cnindex": "A",
    "shfe": "A",
    "ine": "A",
    "czce": "A",
    "cffex": "A",
    "gfex": "A",
    "sge": "A",
}


def trace_id() -> str:
    tid = _TRACE.get()
    if not tid:
        tid = uuid.uuid4().hex[:16]
        _TRACE.set(tid)
    return tid


def set_trace_id(value: str | None) -> None:
    _TRACE.set(value or uuid.uuid4().hex[:16])


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")


def meta(
    source: str,
    *,
    fetched_at: str | None = None,
    cache_hit: bool = False,
    confidence: str | None = None,
    data_as_of: str | None = None,
    extra: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    out = {
        "source": source,
        "fetched_at": fetched_at or now_iso(),
        "cache_hit": cache_hit,
        "confidence": confidence or CONFIDENCE.get(source, "C"),
        "trace_id": trace_id(),
    }
    if data_as_of:
        out["data_as_of"] = data_as_of
    if extra:
        out.update(extra)
    # Unknown metadata keywords (``source_url``, ``count``, ...) land in the
    # envelope's meta instead of raising, so data helpers can annotate freely.
    out.update({k: v for k, v in fields.items() if v is not None})
    return out


def envelope(data: Any, *, source: str, **kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "meta": meta(source, **kwargs)}


def empty(source: str, note: str = "", **kwargs: Any) -> dict[str, Any]:
    extra = {"note": note} if note else {}
    return envelope([], source=source, extra=extra, **kwargs)


def as_of_filter(
    rows: list[dict[str, Any]],
    data_as_of: str | None,
    *,
    candidate_fields: tuple[str, ...] = (
        "crawl_time",
        "publish_time",
        "notice_date",
        "NOTICE_DATE",
        "date",
        "DATE",
    ),
) -> list[dict[str, Any]]:
    """Drop records published after ``data_as_of`` (point-in-time reads)."""
    if not data_as_of:
        return rows
    cutoff = str(data_as_of)[:19].replace("T", " ")
    out: list[dict[str, Any]] = []
    for row in rows:
        stamp = None
        for field in candidate_fields:
            if row.get(field):
                stamp = str(row[field])[:19].replace("T", " ")
                break
        if stamp is None or stamp <= cutoff:
            out.append(row)
    return out
