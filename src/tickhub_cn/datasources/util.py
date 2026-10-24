"""Small helpers shared by the data-source layer."""

from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, Iterable

from ..core.errors import TickHubError
from ..core.provenance import envelope
from ..core.ticker import normalize_code

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def to_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "" or value == "-":
        return default
    if isinstance(value, (int, float)):
        f = float(value)
        return None if math.isnan(f) else f
    text = str(value).replace(",", "").replace("%", "").strip()
    m = _NUM_RE.search(text)
    if not m:
        return default
    try:
        return float(m.group(0))
    except ValueError:
        return default


def to_int(value: Any, default: int | None = None) -> int | None:
    f = to_float(value)
    return default if f is None else int(f)


def code_or_raise(code: str) -> str:
    try:
        return normalize_code(code)
    except ValueError as exc:
        raise TickHubError("QFSD-9001", str(exc)) from exc


def stock_code(value: str) -> str:
    """Single-stock code; rejects the explicit ``sh000001``-style index spelling.

    ``000xxx`` is shared between Shanghai indices and Shenzhen stocks, so an
    explicit Shanghai marker there can only mean an index. Silently normalising
    it would return another company's data.
    """
    raw = str(value or "").strip().lower()
    code = code_or_raise(value)
    explicit_sh = raw.startswith(("sh", "1.")) or raw.endswith((".xshg", ".sh"))
    if explicit_sh and code.startswith("000"):
        raise TickHubError("QFSD-9001", f"{value} 指向沪市指数而非个股（沪市无 000xxx 个股），本接口只服务个股")
    return code


def require_date(value: str | None, default: str | None = None) -> str:
    from ..core.ticker import parse_date

    try:
        return parse_date(value, default)
    except ValueError as exc:
        raise TickHubError("QFSD-9001", str(exc)) from exc


def empty(source: str, note: str = "", **meta_extra: Any) -> dict[str, Any]:
    extra = dict(meta_extra)
    if note:
        extra["note"] = note
    return envelope([], source=source, extra=extra)


def ok(data: Any, source: str, **kwargs: Any) -> dict[str, Any]:
    return envelope(data, source=source, **kwargs)


def frame(rows: Iterable[dict[str, Any]], source: str, **meta_extra: Any) -> dict[str, Any]:
    rows = list(rows)
    extra = dict(meta_extra)
    extra.setdefault("count", len(rows))
    return envelope(rows, source=source, extra=extra)


def now_shanghai() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))


def last_trading_day(anchor: _dt.date | None = None) -> str:
    """Previous weekday (holiday calendar is served by the calendar tool)."""
    day = anchor or now_shanghai().date()
    if anchor is None and now_shanghai().hour < 9:
        day -= _dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= _dt.timedelta(days=1)
    return day.isoformat()


def num(value: Any) -> float | None:
    return to_float(value)


def paginate(rows: list[Any], page: int = 1, page_size: int = 50) -> dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 50), 500))
    start = (page - 1) * page_size
    return {
        "items": rows[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": len(rows),
        "has_more": start + page_size < len(rows),
    }


def js_json(text: str) -> Any:
    """Extract the JSON payload from a ``var x = {...};`` response.

    Sina appends a licence comment (``/* ... */``) after the payload, so decode
    the first JSON value and ignore whatever trails it.
    """
    import json

    text = text.strip()
    if text.startswith("var ") and "=" in text:
        text = text.split("=", 1)[1].strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0] or [-1])
    if start > 0:
        text = text[start:]
    value, _ = json.JSONDecoder().raw_decode(text)
    return value


def as_float_rows(rows: list[dict[str, Any]], fields: Iterable[str]) -> list[dict[str, Any]]:
    names = tuple(fields)
    out = []
    for row in rows:
        item = dict(row)
        for name in names:
            if name in item:
                item[name] = to_float(item[name])
        out.append(item)
    return out


__all__ = [
    "as_float_rows",
    "code_or_raise",
    "empty",
    "frame",
    "js_json",
    "last_trading_day",
    "now_shanghai",
    "num",
    "ok",
    "paginate",
    "require_date",
    "stock_code",
    "to_float",
    "to_int",
]
