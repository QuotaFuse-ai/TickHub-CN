"""Financial statements, indicators, valuation history and stock profile."""

from __future__ import annotations

import datetime as _dt
from typing import Any

from ..core.errors import TickHubError
from ..core.http import em_datacenter, em_get, request
from ..core.ticker import eastmoney_secid, market_of, normalize_code
from ..validation.financial import crosscheck, normalize_report
from .base import tool
from .util import code_or_raise, empty, ok, require_date, stock_code, to_float, to_int

_SINA_FINANCE = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
_SINA_SOURCES = {"balance": "fzb", "income": "lrb", "cashflow": "llb"}

_REPORTS = {
    "balance": "RPT_DMSK_FN_BALANCE",
    "income": "RPT_DMSK_FN_INCOME",
    "cashflow": "RPT_DMSK_FN_CASHFLOW",
}

_FIN_FIELDS = {
    "balance": [
        ("TOTAL_ASSETS", "assets"),
        ("TOTAL_LIABILITIES", "liabilities"),
        ("TOTAL_EQUITY", "equity"),
        ("MONETARYFUNDS", "cash"),
        ("INVENTORY", "inventory"),
        ("ACCOUNTS_RECE", "accounts_receivable"),
        ("FIXED_ASSET", "fixed_assets"),
        ("GOODWILL", "goodwill"),
    ],
    "income": [
        ("TOTAL_OPERATE_INCOME", "revenue"),
        ("OPERATE_COST", "operating_cost"),
        ("OPERATE_PROFIT", "operating_profit"),
        ("TOTAL_PROFIT", "total_profit"),
        ("INCOME_TAX", "income_tax"),
        ("NETPROFIT", "net_profit"),
        ("PARENT_NETPROFIT", "parent_net_profit"),
        ("DEDUCT_PARENT_NETPROFIT", "deducted_net_profit"),
        ("BASIC_EPS", "eps"),
    ],
    "cashflow": [
        ("NETCASH_OPERATE", "net_cash_operate"),
        ("NETCASH_INVEST", "net_cash_invest"),
        ("NETCASH_FINANCE", "net_cash_finance"),
        ("CCE_ADD", "net_cash_increase"),
        ("END_CCE", "ending_cash"),
        ("BEGIN_CCE", "beginning_cash"),
    ],
}


_HOLDER_REPORTS = {"total": "RPT_F10_EH_HOLDERS", "float": "RPT_F10_EH_FREEHOLDERS"}
_MAINOP_TYPES = (("1", "by_industry"), ("2", "by_product"), ("3", "by_region"))


def _period_key(row: dict[str, Any], column: str) -> str:
    return str(row.get(column) or "")[:10]


def _pick(row: dict[str, Any], pairs: list[tuple[str, str]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for source, target in pairs:
        if source in row:
            out[target] = to_float(row.get(source))
    for key in ("REPORT_DATE", "REPORTDATE", "REPORT_DATE_NAME", "SECURITY_CODE", "SECURITY_NAME_ABBR"):
        if key in row:
            out[key.lower()] = row.get(key)
    return out


def _derive_income(row: dict[str, Any]) -> dict[str, Any]:
    """Fill the two income identities the Eastmoney feed omits.

    ``RPT_DMSK_FN_INCOME`` reports no total net profit and no gross profit, but
    both follow from line items it does report (and ``net_profit`` is the exact
    identity ``finance_crosscheck`` verifies). Derived values are only added
    when both operands are present, so a missing input stays missing instead of
    becoming zero.
    """
    if row.get("net_profit") is None:
        total, tax = row.get("total_profit"), row.get("income_tax")
        if total is not None and tax is not None:
            row["net_profit"] = round(total - tax, 2)
    if row.get("gross_profit") is None:
        revenue, cost = row.get("revenue"), row.get("operating_cost")
        if revenue is not None and cost is not None:
            row["gross_profit"] = round(revenue - cost, 2)
    return row


def _finance_rows(code: str, statement: str, *, periods: int, report_date: str = "") -> list[dict[str, Any]]:
    if statement not in _REPORTS:
        raise TickHubError("QFSD-9001", f"unsupported statement: {statement} (balance/income/cashflow)")
    filt = f'(SECURITY_CODE="{normalize_code(code)}")'
    if report_date:
        filt += f'(REPORT_DATE=\'{require_date(report_date)} 00:00:00\')'
    rows = em_datacenter(
        _REPORTS[statement],
        filter_str=filt,
        page_size=max(1, min(int(periods or 8), 40)),
        sort_columns="REPORT_DATE",
        sort_types="-1",
    )
    picked = [_pick(r, _FIN_FIELDS[statement]) for r in rows]
    if statement == "income":
        picked = [_derive_income(r) for r in picked]
    return picked


@tool("eastmoney_financial_report", layer="financials", ttl="semi_static", cache_domain="finance")
def eastmoney_financial_report(code: str, statement: str = "income", periods: int = 8, report_date: str = "") -> dict[str, Any]:
    """Financial statements (balance / income / cashflow) from Eastmoney datacenter."""
    c = code_or_raise(code)
    rows = _finance_rows(c, statement, periods=periods, report_date=report_date)
    return ok({"code": c, "statement": statement, "reports": rows}, "eastmoney", extra={"count": len(rows)})


@tool("eastmoney_financial_all", layer="financials", ttl="semi_static", cache_domain="finance")
def eastmoney_financial_all(code: str, periods: int = 4) -> dict[str, Any]:
    """All three statements plus key indicators for the latest periods."""
    c = code_or_raise(code)
    try:
        indicators = _indicator_rows(c, periods)
    except TickHubError:
        indicators = []
    return ok(
        {
            "code": c,
            "balance": _finance_rows(c, "balance", periods=periods),
            "income": _finance_rows(c, "income", periods=periods),
            "cashflow": _finance_rows(c, "cashflow", periods=periods),
            "indicators": indicators,
        },
        "eastmoney",
    )


def _indicator_rows(code: str, periods: int) -> list[dict[str, Any]]:
    rows = em_datacenter(
        "RPT_F10_FINANCE_MAINFINADATA",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=max(1, min(int(periods or 8), 40)),
        sort_columns="REPORT_DATE",
        sort_types="-1",
    )
    return [
        {
            "report_date": row.get("REPORT_DATE") or row.get("REPORTDATE"),
            "report_name": row.get("REPORT_DATE_NAME"),
            "eps": to_float(row.get("EPSJB")),
            "bps": to_float(row.get("BPS")),
            "revenue": to_float(row.get("TOTALOPERATEREVE")),
            "net_profit": to_float(row.get("PARENTNETPROFIT")),
            "roe": to_float(row.get("ROEJQ") or row.get("ROE")),
            "gross_margin": to_float(row.get("XSMLL")),
            "net_margin": to_float(row.get("XSJLL")),
            "revenue_yoy": to_float(row.get("TOTALOPERATEREVETZ")),
            "profit_yoy": to_float(row.get("PARENTNETPROFITTZ")),
        }
        for row in rows
    ]


@tool("eastmoney_finance_indicators", layer="financials", ttl="semi_static", cache_domain="finance")
def eastmoney_finance_indicators(code: str, periods: int = 8) -> dict[str, Any]:
    """Key financial indicators (EPS, BPS, revenue, net profit, ROE)."""
    c = code_or_raise(code)
    mapped = _indicator_rows(c, periods)
    return ok({"code": c, "indicators": mapped}, "eastmoney", extra={"count": len(mapped)})


@tool("finance_crosscheck", layer="financials", ttl="semi_static", cache_domain="finance")
def finance_crosscheck(code: str, report_date: str = "", tolerance: float = 0.01, strict: bool = True) -> dict[str, Any]:
    """Cross-check financial statements (assets = liabilities + equity, etc.)."""
    c = code_or_raise(code)
    balance = _finance_rows(c, "balance", periods=1, report_date=report_date)
    income = _finance_rows(c, "income", periods=1, report_date=report_date)
    cash = _finance_rows(c, "cashflow", periods=1, report_date=report_date)
    if not balance:
        raise TickHubError("QFSD-9005", f"no financial report for {c}", detail={"report_date": report_date})
    merged: dict[str, Any] = {}
    for part in (balance[0], income[0] if income else {}, cash[0] if cash else {}):
        merged.update({k: v for k, v in part.items() if v is not None})
    values = normalize_report(merged)
    result = crosscheck(values, tolerance=float(tolerance), strict=bool(strict))
    payload = ok(
        {
            "code": c,
            "report_date": balance[0].get("report_date"),
            "values": values,
            "crosscheck": result,
        },
        "computed",
        data_as_of=report_date or None,
    )
    if result["status"] == "fail":
        payload["ok"] = False
        payload["meta"]["error_code"] = "QFSD-4001"
        payload["meta"]["retryable"] = False
    return payload


def _pct(value: Any) -> float | None:
    """Eastmoney F10 ratios arrive as fractions (0.7018), unlike quote fields."""
    f = to_float(value)
    return None if f is None else round(f * 100, 2)


@tool("main_business", layer="financials", ttl="semi_static", cache_domain="finance")
def main_business(code: str, periods: int = 1) -> dict[str, Any]:
    """Main business breakdown by industry, product and region (Eastmoney F10)."""
    c = stock_code(code)
    limit = max(1, min(int(periods or 1), 8))
    rows = em_datacenter(
        "RPT_F10_FN_MAINOP",
        filter_str=f'(SECURITY_CODE="{c}")',
        page_size=min(500, max(60, limit * 60)),
        sort_columns="REPORT_DATE,MAINOP_TYPE",
        sort_types="-1,1",
    )
    if not rows:
        return empty("eastmoney", f"no main-business breakdown for {c}", code=c)
    dates: list[str] = []
    for row in rows:
        date = _period_key(row, "REPORT_DATE")
        if date and date not in dates:
            dates.append(date)
    name = next((row.get("SECURITY_NAME_ABBR") for row in rows if row.get("SECURITY_NAME_ABBR")), None)
    reports = []
    for date in dates[:limit]:
        bucket = [row for row in rows if _period_key(row, "REPORT_DATE") == date]
        entry: dict[str, Any] = {
            "report_date": date,
            "report_name": next((row.get("REPORT_NAME") for row in bucket if row.get("REPORT_NAME")), None),
        }
        for type_code, key in _MAINOP_TYPES:
            items = [row for row in bucket if str(row.get("MAINOP_TYPE") or "") == type_code]
            if not items:
                continue
            items.sort(key=lambda row: to_float(row.get("RANK")) or 0)
            entry[key] = [
                {
                    "item": row.get("ITEM_NAME"),
                    "revenue": to_float(row.get("MAIN_BUSINESS_INCOME")),
                    "revenue_ratio": _pct(row.get("MBI_RATIO")),
                    "cost": to_float(row.get("MAIN_BUSINESS_COST")),
                    "gross_profit": to_float(row.get("MAIN_BUSINESS_RPOFIT")),
                    "gross_margin": _pct(row.get("GROSS_RPOFIT_RATIO")),
                    "rank": to_int(row.get("RANK")),
                }
                for row in items
            ]
        reports.append(entry)
    return ok({"code": c, "name": name, "reports": reports}, "eastmoney", extra={"count": len(reports)})


@tool("top_shareholders", layer="financials", ttl="semi_static", cache_domain="finance")
def top_shareholders(code: str, holder_type: str = "total", periods: int = 1) -> dict[str, Any]:
    """Top-10 shareholders (total or free-float) with position changes (Eastmoney F10)."""
    c = stock_code(code)
    kind = str(holder_type or "total").strip().lower()
    report = _HOLDER_REPORTS.get(kind)
    if not report:
        raise TickHubError("QFSD-9001", f"unsupported holder_type: {holder_type} (total/float)")
    limit = max(1, min(int(periods or 1), 8))
    rows = em_datacenter(
        report,
        filter_str=f'(SECURITY_CODE="{c}")',
        page_size=min(200, max(10, limit * 10)),
        sort_columns="END_DATE,HOLDER_RANK",
        sort_types="-1,1",
    )
    if not rows:
        return empty("eastmoney", f"no top-10 shareholder report for {c}", code=c)
    dates: list[str] = []
    for row in rows:
        date = _period_key(row, "END_DATE")
        if date and date not in dates:
            dates.append(date)
    name = next((row.get("SECURITY_NAME_ABBR") for row in rows if row.get("SECURITY_NAME_ABBR")), None)
    reports = []
    for date in dates[:limit]:
        holders = []
        for row in (row for row in rows if _period_key(row, "END_DATE") == date):
            item: dict[str, Any] = {
                "rank": to_int(row.get("HOLDER_RANK")),
                "name": row.get("HOLDER_NAME"),
                "hold_num": to_float(row.get("HOLD_NUM")),
                "hold_ratio": to_float(row.get("HOLD_NUM_RATIO") if kind == "total" else row.get("HOLD_RATIO")),
                "change": row.get("HOLD_NUM_CHANGE"),
                "change_ratio": to_float(row.get("CHANGE_RATIO")),
                "is_institution": str(row.get("IS_HOLDORG") or "") == "1",
            }
            if kind == "float":
                item["float_ratio"] = to_float(row.get("FREE_HOLDNUM_RATIO"))
                item["holder_type"] = row.get("HOLDER_TYPE")
            holders.append(item)
        holders.sort(key=lambda item: item["rank"] or 0)
        reports.append({"report_date": date, "holders": holders})
    return ok(
        {"code": c, "name": name, "holder_type": kind, "reports": reports},
        "eastmoney",
        extra={"count": len(reports)},
    )


@tool("sina_financial_report", layer="financials", ttl="semi_static", cache_domain="finance")
def sina_financial_report(code: str, statement: str = "income", periods: int = 8) -> dict[str, Any]:
    """Financial statements from Sina (secondary source).

    Sina's download endpoint (``vDOWN_ProfitStatement``) answers HTTP 200 with an
    empty body, so every statement goes through the open API instead. That
    endpoint returns ``report_list`` keyed by report period, each period holding
    the statement line items.
    """
    c = code_or_raise(code)
    source = _SINA_SOURCES.get(statement)
    if not source:
        raise TickHubError("QFSD-9001", f"unsupported statement: {statement} (balance/income/cashflow)")
    market = market_of(c)
    if market == "bj":
        return empty("sina", "sina does not publish BSE financial statements", code=c)
    size = max(1, min(int(periods or 8), 40))
    try:
        payload = request(
            "sina",
            "GET",
            _SINA_FINANCE,
            params={
                "paperCode": f"{market}{c}",
                "source": source,
                "type": "0",
                "page": "1",
                "num": str(size),
            },
        )
    except TickHubError as exc:
        return empty("sina", f"sina financial report unavailable: {exc.message}", code=c)
    data = (((payload or {}).get("result") or {}).get("data") or {})
    report_list = data.get("report_list") or {}
    names = {
        str(entry.get("date_value")): entry.get("date_description")
        for entry in data.get("report_date") or []
        if isinstance(entry, dict)
    }
    reports = []
    for period in sorted(report_list, reverse=True)[:size]:
        obj = report_list[period] or {}
        values: dict[str, Any] = {}
        yoy: dict[str, Any] = {}
        for item in obj.get("data") or []:
            title = str(item.get("item_title") or "").strip()
            raw = item.get("item_value")
            if not title or raw in (None, ""):
                continue
            values[title] = to_float(raw)
            change = item.get("item_tongbi")
            if change not in (None, ""):
                yoy[title] = to_float(change)
        if not values:
            continue
        reports.append(
            {
                "report_date": f"{period[:4]}-{period[4:6]}-{period[6:8]}",
                "report_name": names.get(period),
                "values": values,
                "yoy": yoy,
            }
        )
    if not reports:
        return empty("sina", f"no sina financial report for {c}", code=c)
    return ok(
        {"code": c, "statement": statement, "reports": reports},
        "sina",
        extra={"count": len(reports), "periods": len(report_list)},
    )


def _tencent_profile(c: str) -> dict[str, Any]:
    """Fallback profile when the Eastmoney push endpoint is unreachable."""
    from .quotes import _quote_rows

    rows = _quote_rows([c])
    if not rows:
        raise TickHubError("QFSD-9005", f"no profile for {c}")
    q = rows[0]
    price = to_float(q.get("price"))
    total_cap = to_float(q.get("total_market_cap"))
    float_cap = to_float(q.get("float_market_cap"))
    return ok(
        {
            "code": q.get("code") or c,
            "name": q.get("name"),
            "total_shares": round(total_cap * 1e8 / price) if total_cap and price else None,
            "float_shares": round(float_cap * 1e8 / price) if float_cap and price else None,
            "total_market_cap": total_cap * 1e8 if total_cap else None,
            "float_market_cap": float_cap * 1e8 if float_cap else None,
            "pe": to_float(q.get("pe")),
            "pb": to_float(q.get("pb")),
            "turnover_rate": to_float(q.get("turnover_rate")),
            "change_pct": to_float(q.get("change_pct")),
            "industry": None,
            "region": None,
        },
        "tencent",
        extra={"fallback_from": "eastmoney"},
    )


@tool("stock_profile", layer="financials", ttl="static", cache_domain="profile")
def stock_profile(code: str) -> dict[str, Any]:
    """Stock profile: name, industry, market cap, float shares, PE/PB."""
    c = code_or_raise(code)
    secid = eastmoney_secid(c)
    try:
        data = em_get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={
                "secid": secid,
                "fields": "f57,f58,f84,f85,f116,f117,f162,f167,f168,f170,f127,f128,f129,f100",
            },
            retries=0,
        )
    except TickHubError:
        return _tencent_profile(c)
    d = (data or {}).get("data") or {}
    if not d:
        return _tencent_profile(c)
    return ok(
        {
            "code": d.get("f57") or c,
            "name": d.get("f58"),
            "total_shares": to_float(d.get("f84")),
            "float_shares": to_float(d.get("f85")),
            "total_market_cap": to_float(d.get("f116")),
            "float_market_cap": to_float(d.get("f117")),
            "pe": to_float(d.get("f162")),
            "pb": to_float(d.get("f167")),
            "turnover_rate": to_float(d.get("f168")),
            "change_pct": to_float(d.get("f170")),
            "industry": d.get("f127"),
            "region": d.get("f128"),
        },
        "eastmoney",
    )


def _baostock():
    try:
        import baostock as bs  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise TickHubError("QFSD-9002", "baostock is not installed (pip install baostock)", detail=str(exc)) from exc
    return bs


def _bs_code(code: str) -> str:
    c = normalize_code(code)
    m = market_of(c)
    if m == "bj":
        raise TickHubError("QFSD-9002", "baostock 不支持北交所")
    return f"{m}.{c}"


@tool("baostock_valuation_history", layer="financials", ttl="static", cache_domain="valuation")
def baostock_valuation_history(code: str, start: str = "", end: str = "") -> dict[str, Any]:
    """Historical PE/PB/PS/PCF and turnover (baostock, no BSE)."""
    bs = _baostock()
    c = _bs_code(code)
    end_date = require_date(end or None, _dt.date.today().isoformat())
    start_date = require_date(start or None, (_dt.date.today() - _dt.timedelta(days=365)).isoformat())
    lg = bs.login()
    if lg.error_code != "0":
        raise TickHubError("QFSD-9004", f"baostock login failed: {lg.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            c,
            "date,close,peTTM,pbMRQ,psTTM,pcfNcfTTM,turn,tradestatus",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="3",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            date, close, pe, pb, ps, pcf, turn, status = rs.get_row_data()
            rows.append(
                {
                    "date": date,
                    "close": to_float(close),
                    "pe_ttm": to_float(pe),
                    "pb": to_float(pb),
                    "ps_ttm": to_float(ps),
                    "pcf_ttm": to_float(pcf),
                    "turnover_rate": to_float(turn),
                    "tradestatus": status,
                }
            )
        return ok({"code": normalize_code(code), "rows": rows}, "baostock", extra={"count": len(rows)})
    finally:
        bs.logout()


@tool("baostock_stock_basic", layer="financials", ttl="static", cache_domain="profile")
def baostock_stock_basic(code: str) -> dict[str, Any]:
    """Listing / delisting dates and status (baostock)."""
    bs = _baostock()
    c = _bs_code(code)
    lg = bs.login()
    if lg.error_code != "0":
        raise TickHubError("QFSD-9004", f"baostock login failed: {lg.error_msg}")
    try:
        rs = bs.query_stock_basic(code=c)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(dict(zip(rs.fields, rs.get_row_data())))
        if not rows:
            raise TickHubError("QFSD-9005", f"no baostock basic info for {code}")
        return ok(rows[0], "baostock")
    finally:
        bs.logout()


@tool("baostock_st_list", layer="financials", ttl="static", cache_domain="profile")
def baostock_st_list() -> dict[str, Any]:
    """Historical ST / *ST records (baostock)."""
    bs = _baostock()
    lg = bs.login()
    if lg.error_code != "0":
        raise TickHubError("QFSD-9004", f"baostock login failed: {lg.error_msg}")
    try:
        rs2 = bs.query_stock_st_list()
        rows = []
        while rs2.error_code == "0" and rs2.next():
            rows.append(dict(zip(rs2.fields, rs2.get_row_data())))
        return ok(rows, "baostock", extra={"count": len(rows)})
    finally:
        bs.logout()


__all__ = [
    "baostock_st_list",
    "baostock_stock_basic",
    "baostock_valuation_history",
    "eastmoney_finance_indicators",
    "eastmoney_financial_all",
    "eastmoney_financial_report",
    "finance_crosscheck",
    "main_business",
    "sina_financial_report",
    "stock_profile",
    "top_shareholders",
]
