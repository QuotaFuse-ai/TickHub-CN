"""Financial statements, indicators, valuation history and stock profile."""

from __future__ import annotations

import datetime as _dt
import io
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..core.errors import TickHubError
from ..core.http import em_datacenter, em_get, fetch, request
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


_PDF_MAX_PAGES = 400
# Most specific first: the audit report also says 「财务报表附注」, while the
# notes chapter itself is headed 「合并财务报表项目注释」 in most annual reports.
_NOTES_HEADINGS = ("合并财务报表项目注释", "财务报表项目注释", "财务报表附注", "财务报告")

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _text_layer_readable(pages: list[str]) -> bool:
    """False when the PDF has no text layer, or its fonts carry no Unicode map.

    Annual reports typeset with a legacy Adobe-GB1 CID font extract as pages of
    Latin/Hangul code points when the engine lacks the CID-to-Unicode table;
    a scanned report extracts as nothing at all.
    """
    text = "".join(pages)
    if not text.strip():
        return False
    if len(text) < 2000:
        return True
    return len(_CJK_RE.findall(text)) / len(text) >= 0.02


def _pypdf_pages(blob: bytes) -> list[str] | None:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        return None
    # pypdf warns once per font it cannot decode without fontTools; a legacy CID
    # report emits hundreds of these and drowns the server log.
    pypdf_log = logging.getLogger("pypdf")
    level = pypdf_log.level
    pypdf_log.setLevel(logging.ERROR)
    try:
        reader = PdfReader(io.BytesIO(blob))
        pages: list[str] = []
        for page in reader.pages[:_PDF_MAX_PAGES]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - one broken page must not sink the report
                pages.append("")
        return pages
    except Exception:  # noqa: BLE001 - another engine may still parse the file
        return None
    finally:
        pypdf_log.setLevel(level)


def _pdfminer_pages(blob: bytes) -> list[str] | None:
    """Page texts via pdfminer.six, which bundles the Adobe-GB1 UCS-2 table."""
    try:
        from pdfminer.high_level import extract_text  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        text = extract_text(io.BytesIO(blob), maxpages=_PDF_MAX_PAGES)
    except Exception:  # noqa: BLE001 - treated as "engine unavailable"
        return None
    pages = text.split("\f")
    while pages and not pages[-1].strip():
        pages.pop()
    return pages or None


def _pdftotext_pages(blob: bytes) -> list[str] | None:
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "report.pdf"
        src.write_bytes(blob)
        out = Path(tmp) / "report.txt"
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [exe, "-layout", "-f", "1", "-l", str(_PDF_MAX_PAGES), str(src), str(out)],
            capture_output=True,
            timeout=180,
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "ignore").strip()[:200]
            raise TickHubError("QFSD-9006", f"pdftotext 解析失败: {detail}")
        text = out.read_text("utf-8", errors="ignore")
    return text.split("\f")


def _pdf_pages(blob: bytes) -> tuple[list[str], str]:
    """Page texts for a PDF blob, plus the engine that produced them.

    pypdf is fastest but carries no Adobe-GB1 table; pdfminer.six does and is
    the fallback for reports set in legacy CID fonts. pdftotext comes last
    because the copy shipped with Git for Windows is Xpdf 4.0 without CJK CMaps.
    """
    unreadable: list[tuple[list[str], str]] = []
    for reader, engine in ((_pypdf_pages, "pypdf"), (_pdfminer_pages, "pdfminer")):
        pages = reader(blob)
        if not pages:
            continue
        if _text_layer_readable(pages):
            return pages, engine
        unreadable.append((pages, engine))

    text_error: TickHubError | None = None
    try:
        text_pages = _pdftotext_pages(blob)
    except TickHubError as exc:
        text_pages, text_error = None, exc
    if text_pages:
        if _text_layer_readable(text_pages):
            return text_pages, "pdftotext"
        unreadable.append((text_pages, "pdftotext"))

    if unreadable:
        # Every engine produced mojibake: hand back the CMap-aware one and let
        # the caller flag the text layer as unreadable.
        return next((item for item in unreadable if item[1] == "pdfminer"), unreadable[0])
    if text_error is not None:
        raise text_error
    raise TickHubError(
        "QFSD-9006",
        "PDF 正文解析需要 pypdf 或 pdfminer.six（pip install 'tickhub-cn[notes]'），"
        "或系统 pdftotext",
    )


def _squash(text: str) -> str:
    """Drop whitespace so a heading split across text containers still matches."""
    return "".join(text.split())


def _notes_excerpt(pages: list[str], chars: int) -> dict[str, Any]:
    """Locate the notes section and cut ``chars`` characters from its start.

    Annual reports list the notes in the table of contents long before the
    section itself, so hits in the first third of the file are skipped when a
    later one exists.
    """
    total = len(pages)
    floor = max(3, total // 3)
    flat = [_squash(text) for text in pages]
    for heading in _NOTES_HEADINGS:
        hits = [i for i, text in enumerate(flat) if heading in text]
        if not hits:
            continue
        after_toc = [i for i in hits if i >= floor] or hits
        # The chapter page either *opens* with the heading or prints it in the
        # running footer — bank annual reports put 「第八章 财务报告 财务报表附注」
        # at the bottom of every chapter page. A hit in the middle of a page is
        # usually a cross-reference: the audit report cites the notes chapter
        # well before it starts.
        top = [
            i
            for i in after_toc
            if flat[i].find(heading) <= max(80, len(flat[i]) // 7)
            or flat[i].rfind(heading) >= len(flat[i]) * 0.85
        ]
        start = top[0] if top else after_toc[0]
        body = "\n".join(pages[start:]).strip()
        return {
            "found": True,
            "heading": heading,
            "start_page": start + 1,
            "text": body[:chars],
            "truncated": len(body) > chars,
        }
    return {"found": False, "reason": "报告正文中未出现财务报表附注章节标题"}


def _extract_notes(report: dict[str, Any], chars: int) -> tuple[dict[str, Any], str | None]:
    """Fetch one annual-report PDF and pull its notes section.

    Returns ``(notes, failure)``; a failure keeps the locator envelope intact.
    """
    url = str(report.get("pdf") or report.get("url") or "")
    if not url:
        return {"found": False, "reason": "该报告没有可下载的 PDF 链接"}, "该报告没有可下载的 PDF 链接"
    try:
        blob = fetch("cninfo", "GET", url).content
        pages, engine = _pdf_pages(blob)
    except TickHubError as exc:
        return {"found": False, "reason": str(exc)}, str(exc)
    except Exception as exc:  # noqa: BLE001 - extraction is an add-on, never fatal
        reason = f"PDF 正文解析失败: {exc}"[:200]
        return {"found": False, "reason": reason}, reason
    where = {"url": url, "title": report.get("title"), "engine": engine, "pages": len(pages)}
    if not _text_layer_readable(pages):
        if not "".join(pages).strip():
            reason = "年报 PDF 没有文本层（扫描件），需 OCR 才能还原正文"
        else:
            reason = (
                "年报 PDF 的字体缺少 Unicode 映射（多为 Adobe-GB1 旧编码），"
                "pypdf、pdfminer.six 与 pdftotext 均未能还原中文；"
                "请直接打开 PDF 查阅附注，或用 PaddleOCR/MinerU 一类工具识别"
            )
        return {**where, "found": False, "unreadable": True, "reason": reason}, reason
    excerpt = _notes_excerpt(pages, chars)
    return {**where, **excerpt}, None


@tool(layer="financials", ttl="semi_static", cache_domain="financial_notes")
def cninfo_financial_notes(
    code: str, year: str = "", limit: int = 5, extract: bool = False, chars: int = 8000
) -> dict[str, Any]:
    """Locate annual and audit reports that carry the financial-statement notes (CNINFO PDFs)."""
    from .announcements import _cninfo_rows

    c = stock_code(code)
    year_text = str(year or "").strip()
    if year_text and not (len(year_text) == 4 and year_text.isdigit()):
        raise TickHubError("QFSD-9001", "year must be a four-digit fiscal year such as 2024")

    annual, _ = _cninfo_rows(c, "", "", "category_ndbg_szsh", 1, 30)
    reports: list[dict[str, Any]] = []
    for row in annual:
        title = str(row.get("title") or "")
        if "摘要" in title or "英文" in title:
            continue
        if "年度报告" not in title and "年报" not in title:
            continue
        match = re.search(r"(\d{4})", title)
        fiscal = match.group(1) if match else ""
        if year_text and fiscal != year_text:
            continue
        reports.append({**row, "fiscal_year": fiscal or None})
    reports.sort(key=lambda row: str(row.get("publish_time") or ""), reverse=True)
    if limit and limit > 0:
        reports = reports[: int(limit)]

    audit, _ = _cninfo_rows(c, "", "", "", 1, 20, searchkey="审计报告")
    audit.sort(key=lambda row: str(row.get("publish_time") or ""), reverse=True)
    if year_text:
        audit = [row for row in audit if year_text in str(row.get("title") or "")]

    if not reports:
        scope = f"{year_text} 年度" if year_text else "最近年度"
        return empty("cninfo", f"{c} 未找到{scope}报告，无法定位财务报表附注")

    payload: dict[str, Any] = {"code": c, "year": year_text or None, "reports": reports, "audit_reports": audit[:3]}
    extra: dict[str, Any] = {
        "count": len(reports),
        "note": "财务报表附注随年度报告 PDF 一并发布，巨潮资讯不提供附注正文的独立接口；请打开年度报告 PDF 查阅附注。",
    }
    if extract:
        notes, failure = _extract_notes(reports[0], max(500, int(chars or 8000)))
        payload["notes"] = notes
        if failure:
            extra["degraded"] = "notes_text_layer_unreadable" if notes.get("unreadable") else "notes_extract_failed"
            extra["note"] = f"{failure}；已回落到 PDF 定位结果。"
        elif notes.get("found"):
            extra["note"] = (
                f"附注正文自年报 PDF 第 {notes['start_page']} 页（「{notes['heading']}」标题命中页）起摘录，"
                "按页拼接、未做表格还原；命中页也可能是引用该章节的审计报告页。"
            )
        else:
            extra["degraded"] = "notes_not_found"
            extra["note"] = f"{notes.get('reason')}；已回落到 PDF 定位结果。"
    return ok(payload, "cninfo", extra=extra)


__all__ = [
    "baostock_st_list",
    "baostock_stock_basic",
    "baostock_valuation_history",
    "cninfo_financial_notes",
    "eastmoney_finance_indicators",
    "eastmoney_financial_all",
    "eastmoney_financial_report",
    "finance_crosscheck",
    "main_business",
    "sina_financial_report",
    "stock_profile",
    "top_shareholders",
]
