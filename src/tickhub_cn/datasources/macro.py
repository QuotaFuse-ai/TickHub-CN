"""Macro data: growth, prices, money, rates, bond yields and the calendar."""

from __future__ import annotations

import math
from typing import Any

from ..core.errors import TickHubError
from ..core.http import em_datacenter, request
from .base import tool
from .util import empty, frame, ok, require_date, to_float

_ECON = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_CHINAMONEY = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-shibor/ShiborHis"


def _records(df: Any) -> list[dict[str, Any]]:
    records = df.to_dict("records")
    out = []
    for row in records:
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, float) and math.isnan(value):
                clean[str(key)] = None
            elif hasattr(value, "isoformat"):
                clean[str(key)] = value.isoformat()
            else:
                clean[str(key)] = value
        out.append(clean)
    return out


def _optional_ak(fn_name: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise TickHubError(
            "QFSD-9002",
            f"akshare is required for {fn_name}",
            detail={"install": "pip install tickhub-cn[sources]"},
        ) from exc
    fn = getattr(ak, fn_name, None)
    if fn is None:
        raise TickHubError("QFSD-9002", f"akshare has no function {fn_name}")
    return _records(fn(*args, **kwargs))


def _econ(report_name: str, page_size: int = 36) -> list[dict[str, Any]]:
    return em_datacenter(
        report_name,
        columns="ALL",
        page_size=min(max(1, int(page_size or 36)), 200),
        sort_columns="REPORT_DATE",
        sort_types="-1",
    )


def _ak_or_empty(source: str, note: str, fn_name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        rows = _optional_ak(fn_name, *args, **kwargs)
    except TickHubError as exc:
        return empty(source, f"{note}（需安装 sources 扩展：{exc.message}）")
    except Exception as exc:  # noqa: BLE001
        return empty(source, f"{note}: {type(exc).__name__}")
    if not rows:
        return empty(source, f"{note}：上游暂无数据")
    return ok(rows, source, extra={"count": len(rows)})


@tool(layer="macro", ttl="static")
def china_gdp(page_size: int = 20) -> dict[str, Any]:
    """China GDP by quarter (累计值, 亿元) with year-on-year growth."""
    rows = _econ("RPT_ECONOMY_GDP", page_size)
    if not rows:
        return empty("stats", "GDP series unavailable")
    out = [
        {
            "period": r.get("TIME"),
            "report_date": str(r.get("REPORT_DATE") or "")[:10],
            "gdp": to_float(r.get("DOMESTICL_PRODUCT_BASE")),
            "primary": to_float(r.get("FIRST_PRODUCT_BASE")),
            "secondary": to_float(r.get("SECOND_PRODUCT_BASE")),
            "tertiary": to_float(r.get("THIRD_PRODUCT_BASE")),
            "gdp_yoy": to_float(r.get("SUM_SAME")),
            "primary_yoy": to_float(r.get("FIRST_SAME")),
            "secondary_yoy": to_float(r.get("SECOND_SAME")),
            "tertiary_yoy": to_float(r.get("THIRD_SAME")),
        }
        for r in rows
    ]
    return frame(out, "stats", source_url="eastmoney/RPT_ECONOMY_GDP")


@tool(layer="macro", ttl="static")
def china_cpi(page_size: int = 36) -> dict[str, Any]:
    """China CPI: year-on-year, month-on-month and cumulative indices."""
    rows = _econ("RPT_ECONOMY_CPI", page_size)
    if not rows:
        return empty("stats", "CPI series unavailable")
    out = [
        {
            "period": r.get("TIME"),
            "report_date": str(r.get("REPORT_DATE") or "")[:10],
            "cpi_yoy": to_float(r.get("NATIONAL_SAME")),
            "cpi_index": to_float(r.get("NATIONAL_BASE")),
            "cpi_mom": to_float(r.get("NATIONAL_SEQUENTIAL")),
            "cpi_ytd": to_float(r.get("NATIONAL_ACCUMULATE")),
            "urban_yoy": to_float(r.get("CITY_SAME")),
            "rural_yoy": to_float(r.get("RURAL_SAME")),
        }
        for r in rows
    ]
    return frame(out, "stats", source_url="eastmoney/RPT_ECONOMY_CPI")


@tool(layer="macro", ttl="static")
def china_pmi(page_size: int = 36) -> dict[str, Any]:
    """China official manufacturing and non-manufacturing PMI."""
    rows = _econ("RPT_ECONOMY_PMI", page_size)
    if not rows:
        return empty("stats", "PMI series unavailable")
    out = [
        {
            "period": r.get("TIME"),
            "report_date": str(r.get("REPORT_DATE") or "")[:10],
            "manufacturing_pmi": to_float(r.get("MAKE_INDEX")),
            "manufacturing_yoy": to_float(r.get("MAKE_SAME")),
            "non_manufacturing_pmi": to_float(r.get("NMAKE_INDEX")),
            "non_manufacturing_yoy": to_float(r.get("NMAKE_SAME")),
        }
        for r in rows
    ]
    return frame(out, "stats", source_url="eastmoney/RPT_ECONOMY_PMI")


@tool(layer="macro", ttl="static")
def china_retail_sales(page_size: int = 36) -> dict[str, Any]:
    """China retail sales of consumer goods."""
    rows = _econ("RPT_ECONOMY_TOTAL_RETAIL", page_size)
    if not rows:
        return empty("stats", "retail series unavailable")
    out = [
        {
            "period": r.get("TIME"),
            "report_date": str(r.get("REPORT_DATE") or "")[:10],
            "retail_total": to_float(r.get("RETAIL_TOTAL")),
            "retail_yoy": to_float(r.get("RETAIL_TOTAL_SAME")),
            "retail_mom": to_float(r.get("RETAIL_TOTAL_SEQUENTIAL")),
            "retail_ytd": to_float(r.get("RETAIL_TOTAL_ACCUMULATE")),
            "retail_ytd_yoy": to_float(r.get("RETAIL_ACCUMULATE_SAME")),
        }
        for r in rows
    ]
    return frame(out, "stats", source_url="eastmoney/RPT_ECONOMY_TOTAL_RETAIL")


@tool(layer="macro", ttl="static")
def china_money_supply(page_size: int = 36) -> dict[str, Any]:
    """China money supply: M0, M1 and M2 (亿元) with growth rates."""
    rows = _econ("RPT_ECONOMY_CURRENCY_SUPPLY", page_size)
    if not rows:
        return empty("pbc", "money supply series unavailable")
    out = [
        {
            "period": r.get("TIME"),
            "report_date": str(r.get("REPORT_DATE") or "")[:10],
            "m0": to_float(r.get("BASIC_CURRENCY")),
            "m0_yoy": to_float(r.get("BASIC_CURRENCY_SAME")),
            "m1": to_float(r.get("CURRENCY")),
            "m1_yoy": to_float(r.get("CURRENCY_SAME")),
            "m2": to_float(r.get("FREE_CASH")),
            "m2_yoy": to_float(r.get("FREE_CASH_SAME")),
        }
        for r in rows
    ]
    return frame(out, "pbc", source_url="eastmoney/RPT_ECONOMY_CURRENCY_SUPPLY")


@tool(layer="macro", ttl="static")
def china_social_financing(months: int = 24) -> dict[str, Any]:
    """Total social financing (社会融资规模) via the optional akshare bridge."""
    return _ak_or_empty("pbc", "社会融资规模", "macro_china_shrzgm")


@tool(layer="macro", ttl="static")
def china_lpr(months: int = 24) -> dict[str, Any]:
    """Loan prime rate (LPR) history via the optional akshare bridge."""
    return _ak_or_empty("pbc", "LPR 报价", "macro_china_lpr")


@tool(layer="macro", ttl="static")
def bond_yield_curve(date: str = "") -> dict[str, Any]:
    """ChinaBond government-bond yield curve via the optional akshare bridge."""
    day = require_date(date) if date else None
    if day:
        return _ak_or_empty("chinabond", "国债收益率曲线", "bond_china_yield", start_date=day, end_date=day)
    return _ak_or_empty("chinabond", "国债收益率曲线", "bond_china_yield")


@tool(layer="macro", ttl="static")
def repo_rate(days: int = 30) -> dict[str, Any]:
    """Interbank repo fixing (DR007 and peers) via the optional akshare bridge."""
    return _ak_or_empty("chinamoney", "银行间回购定盘利率", "repo_rate_hist")


@tool(layer="macro", ttl="static")
def shibor_rates(days: int = 30) -> dict[str, Any]:
    """Shibor term structure (best effort: ChinaMoney page, then akshare)."""
    try:
        payload = request(
            "chinamoney",
            "GET",
            _CHINAMONEY,
            params={"lang": "CN", "startDate": "", "endDate": ""},
            expect_json=True,
        )
        data = (payload or {}).get("data") or {}
        rows = data.get("shiborList") or data.get("records") or []
        if rows:
            return ok(rows, "chinamoney", extra={"count": len(rows)})
    except TickHubError:
        pass
    return _ak_or_empty("chinamoney", "Shibor 报价", "macro_china_shibor_all")


@tool(layer="macro", ttl="static")
def macro_calendar(date: str = "") -> dict[str, Any]:
    """Economic calendar for a date (best effort via the optional akshare bridge)."""
    if date:
        return _ak_or_empty("stats", "财经日历", "news_economic_baidu", date=require_date(date))
    return _ak_or_empty("stats", "财经日历", "news_economic_baidu")


@tool(layer="macro", ttl="static")
def economy_snapshot() -> dict[str, Any]:
    """Latest print of the main activity and price series in one call."""
    snapshot: dict[str, Any] = {}
    for name, fn in (
        ("gdp", china_gdp),
        ("cpi", china_cpi),
        ("pmi", china_pmi),
        ("retail", china_retail_sales),
        ("money_supply", china_money_supply),
    ):
        try:
            payload = fn(page_size=1, _no_cache=True) if name != "gdp" else fn(page_size=1, _no_cache=True)
        except Exception as exc:  # noqa: BLE001
            snapshot[name] = {"error": type(exc).__name__}
            continue
        items = payload.get("data")
        if isinstance(items, dict):
            items = items.get("items")
        snapshot[name] = (items or [None])[0] if isinstance(items, list) else None
    return ok(snapshot, "stats")
