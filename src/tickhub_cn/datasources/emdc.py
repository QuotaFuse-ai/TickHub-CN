"""Shared strict access to Eastmoney's datacenter reports.

The datacenter pages results and silently accepts filters it cannot apply, so
every read here verifies the returned rows against the requested filters and
treats a duplicate page boundary as an incomplete result.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.errors import TickHubError
from ..core.http import em_get
from .util import to_float

DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def limit_value(value: Any, upper: int = 5000) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise TickHubError("QFSD-9001", f"limit 必须是整数: {value!r}") from exc
    if not 1 <= limit <= upper:
        raise TickHubError("QFSD-9001", f"limit 范围 1–{upper}")
    return limit


def day(value: Any) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def num(value: Any) -> float | None:
    return to_float(value)


def fetch_pages(
    report: str,
    filter_str: str,
    sort_columns: str,
    sort_types: str,
    limit: int,
    extra: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Page through a datacenter report until ``limit`` rows or the last page."""
    rows: list[dict[str, Any]] = []
    page_size = min(limit, 500)
    page = 1
    while len(rows) < limit:
        params: dict[str, Any] = {
            "reportName": report,
            "columns": "ALL",
            "filter": filter_str,
            "pageNumber": str(page),
            "pageSize": str(page_size),
            "sortColumns": sort_columns,
            "sortTypes": sort_types,
            "source": "WEB",
            "client": "WEB",
        }
        if extra:
            params.update(extra)
        payload = em_get(DATACENTER, params=params) or {}
        if not payload.get("success", True):
            message = str(payload.get("message") or "")
            # The datacenter reports "no rows matched" as a failure; an empty
            # result set is a normal answer, not a broken interface.
            if "数据为空" in message:
                break
            raise TickHubError(
                "QFSD-9003",
                f"东财 {report} 返回错误: {message}",
                detail={"report": report, "code": payload.get("code")},
            )
        result = payload.get("result") or {}
        batch = result.get("data") or []
        if not batch:
            break
        rows.extend(batch)
        pages = result.get("pages")
        if not pages or page >= int(pages):
            break
        page += 1
    return rows[:limit]


def strict_rows(
    report: str,
    filter_str: str,
    sort_columns: str,
    sort_types: str,
    limit: int,
    *,
    narrowed: bool,
    equal: dict[str, Any] | None = None,
    dates: dict[str, tuple[str, str]] | None = None,
    extra: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Read a report, failing loudly when the interface looks broken or ignores filters.

    ``narrowed=False`` means the whole market was requested with no conditions;
    zero rows then means the interface changed, not that there is no data.
    """
    rows = fetch_pages(report, filter_str, sort_columns, sort_types, limit, extra)
    if not rows and not narrowed:
        raise TickHubError("QFSD-9003", f"东财 {report} 全市场返回 0 行，接口可能已变更")
    seen: set[str] = set()
    for row in rows:
        blob = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        if blob in seen:
            raise TickHubError("QFSD-9003", f"东财 {report} 翻页返回重复行（排序不唯一），结果不完整")
        seen.add(blob)
        for field, value in (equal or {}).items():
            if row.get(field) != value:
                raise TickHubError(
                    "QFSD-9003",
                    f"东财 {report} 请求 {field}={value!r}，却返回了 {row.get(field)!r}",
                )
        for field, (lo, hi) in (dates or {}).items():
            stamp = str(row.get(field) or "")[:10]
            if (lo and stamp < lo) or (hi and stamp > hi):
                raise TickHubError(
                    "QFSD-9003",
                    f"东财 {report} 请求 {field} 在 {lo}~{hi}，却返回了 {stamp}",
                )
    return rows
