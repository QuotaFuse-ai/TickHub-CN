"""Exchange-official backups for the aggregator feeds.

Every tool here answers a question that a commercial aggregator also answers,
but reads it from the exchange's own publication so one vendor outage cannot
blank a dataset. Exchange files are published once per trading day after the
close, so the trade date is an explicit argument and a date the exchange does
not carry is reported as empty instead of being back-filled from elsewhere.

Units are normalised to 元 for amounts and 股 for volumes; the raw unit each
exchange uses is recorded in the response so callers can audit the conversion.

The margin table is the exchange's own list, so it also covers margin-eligible
ETFs and B-shares (``5xxxxx``/``9xxxxx``); callers that want ordinary equities
filter on the code prefix themselves.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from ..core.http import request
from ..core.ticker import compact_date, market_of, normalize_code
from .base import tool
from .util import (
    code_or_raise,
    empty,
    last_trading_day,
    ok,
    require_date,
    to_float,
    to_int,
)

_SSE_MARGIN = "https://query.sse.com.cn/marketdata/tradedata/queryMargin.do"
_SSE_LHB = "https://query.sse.com.cn/infodisplay/showTradePublicFile.do"
_SZSE_REPORT = "https://www.szse.cn/api/report/ShowReport/data"
_SZSE_DOWNLOAD = "https://disc.static.szse.cn/download"
_SZSE_ANNOUNCE = "https://www.szse.cn/api/disc/announcement/annList"
_BSE_QUOTE = "https://www.bse.cn/nqhqController/nqhq_en.do"
_SINA_FLOW_HISTORY = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs"
_EM_ANNOUNCE = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_EM_PDF = "https://pdf.dfcfw.com/pdf/H2_{art}_1.pdf"
_EM_NOTICE = "https://data.eastmoney.com/notices/detail/{code}/{art}.html"

_SZSE_MARGIN_REFERER = "https://www.szse.cn/disclosure/margin/margin/index.html"
_SZSE_LHB_REFERER = "https://www.szse.cn/disclosure/deal/public/index.html"
_SSE_REFERER = "https://www.sse.com.cn/"
_BSE_HEADERS = {
    "Referer": "https://www.bse.cn/nq/quotation.html",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

UNIT_YUAN_SHARE = {"amount": "元", "volume": "股"}

_TAG_RE = re.compile(r"<[^>]+>")
_SSE_SECTION_RE = re.compile(r"^\s*([一二三四五六七八九十]+)、\s*(.+?)\s*[:：]?\s*$")
_SSE_BOARD_RE = re.compile(r"^\s*\d+、\s*(\S+)\s*$")
_SSE_LHB_ROW_RE = re.compile(
    r"^\s*\(\d+\)\s+(\d{6})\s+(\S+)\s+(-?[\d.]+)%\s+([\d,]+)\s+([\d,.]+)(?:\s+(\S+))?\s*$"
)
_SZSE_DETAIL_RE = re.compile(r"a-param='([^']+)'")


def _clean(value: Any) -> str:
    return _TAG_RE.sub("", str(value or "")).replace("&nbsp;", " ").strip()


def _yuan(value: Any, scale: float) -> float | None:
    """Exchange amount in ``scale`` yuan-per-unit -> 元."""
    raw = to_float(value)
    return None if raw is None else round(raw * scale, 2)


def _shares(value: Any, scale: float) -> float | None:
    raw = to_float(value)
    return None if raw is None else round(raw * scale, 0)


def _szse_block(payload: Any) -> dict[str, Any]:
    # ShowReport/data answers with a one-element array of {metadata, data}.
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


# --------------------------------------------------------------------------- margin


def _sse_margin_rows(day: str) -> list[dict[str, Any]]:
    params = {
        "isPagination": "true",
        "tabType": "mxtype",
        "detailsDate": compact_date(day),
        "pageHelp.pageSize": 2000,
        "pageHelp.pageNo": 1,
        "pageHelp.beginPage": 1,
        "pageHelp.cacheSize": 1,
        "pageHelp.endPage": 1,
    }
    payload = request("sse", "GET", _SSE_MARGIN, params=params, headers={"Referer": _SSE_REFERER})
    page = (payload or {}).get("pageHelp") or {}
    rows = [r for r in page.get("data") or [] if isinstance(r, dict)]
    total = to_int(page.get("total"), len(rows)) or len(rows)
    pages = to_int(page.get("pageCount"), 1) or 1
    for number in range(2, min(pages, 10) + 1):
        params["pageHelp.pageNo"] = number
        params["pageHelp.beginPage"] = number
        params["pageHelp.endPage"] = number
        more = request("sse", "GET", _SSE_MARGIN, params=params, headers={"Referer": _SSE_REFERER})
        rows.extend(
            r for r in ((more or {}).get("pageHelp") or {}).get("data") or [] if isinstance(r, dict)
        )
        if len(rows) >= total:
            break
    return [r for r in rows if str(r.get("opDate") or "") == compact_date(day)]


def _map_sse_margin(row: dict[str, Any]) -> dict[str, Any]:
    balance = to_float(row.get("rzye"))
    short_balance = to_float(row.get("rqylje"))
    return {
        "code": normalize_code(row.get("stockCode")),
        "name": _clean(row.get("securityAbbr")),
        "margin_balance": balance,
        "margin_buy": to_float(row.get("rzmre")),
        "margin_repay": to_float(row.get("rzche")),
        "short_balance": short_balance,
        "short_volume": to_float(row.get("rqyl")),
        "short_sell_volume": to_float(row.get("rqmcl")),
        "short_repay_volume": to_float(row.get("rqchl")),
        "total_balance": None if balance is None or short_balance is None else round(balance + short_balance, 2),
    }


def _szse_margin_rows(day: str, code: str, max_pages: int) -> list[dict[str, Any]]:
    params = {"SHOWTYPE": "JSON", "CATALOGID": "1837_xxpl", "TABKEY": "tab2", "txtDate": day}
    headers = {"Referer": _SZSE_MARGIN_REFERER}
    out: list[dict[str, Any]] = []
    pages = 1
    number = 1
    while number <= min(pages, max_pages):
        block = _szse_block(
            request("exchange", "GET", _SZSE_REPORT, params={**params, "PAGENO": number}, headers=headers)
        )
        if number == 1:
            pages = to_int((block.get("metadata") or {}).get("pagecount"), 1) or 1
        batch = [r for r in block.get("data") or [] if isinstance(r, dict)]
        if not batch:
            break
        if not code:
            out.extend(batch)
        else:
            for row in batch:
                current = str(row.get("zqdm") or "")
                if current == code:
                    return [row]
                # tab2 is sorted by code ascending, so once we pass the target
                # it cannot appear on a later page.
                if current > code:
                    return []
        number += 1
    return out


def _szse_margin_summary(day: str) -> dict[str, Any] | None:
    block = _szse_block(
        request(
            "exchange",
            "GET",
            _SZSE_REPORT,
            params={"SHOWTYPE": "JSON", "CATALOGID": "1837_xxpl", "TABKEY": "tab1", "txtDate": day, "PAGENO": 1},
            headers={"Referer": _SZSE_MARGIN_REFERER},
        )
    )
    rows = [r for r in block.get("data") or [] if isinstance(r, dict)]
    if not rows or not rows[0].get("jrrzye"):
        return None
    row = rows[0]
    return {
        "date": day,
        "margin_balance": _yuan(row.get("jrrzye"), 1e8),
        "margin_buy": _yuan(row.get("jrrzmr"), 1e8),
        "short_balance": _yuan(row.get("jrrjye"), 1e8),
        "short_volume": _shares(row.get("jrrjyl"), 1e8),
        "short_sell_volume": _shares(row.get("jrrjmc"), 1e8),
        "total_balance": _yuan(row.get("jrrzrjye"), 1e8),
    }


def _map_szse_margin(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": str(row.get("zqdm") or ""),
        "name": _clean(row.get("zqjc")),
        "margin_balance": _yuan(row.get("jrrzye"), 1e8),
        "margin_buy": _yuan(row.get("jrrzmr"), 1e8),
        "margin_repay": None,
        "short_balance": _yuan(row.get("jrrjye"), 1e8),
        "short_volume": _shares(row.get("jrrjyl"), 1e4),
        "short_sell_volume": _shares(row.get("jrrjmc"), 1e4),
        "short_repay_volume": None,
        "total_balance": _yuan(row.get("jrrzrjye"), 1e8),
    }


def _szse_lag_note(day: str) -> str:
    """SZSE publishes the day's margin table one trading day later than SSE."""
    previous = last_trading_day(_dt.date.fromisoformat(day) - _dt.timedelta(days=1))
    return f"深交所按 T+1 披露，{day} 通常要到下一交易日才有明细；可先查 {previous}（非交易日亦无数据）"


@tool(layer="funds", ttl="semi_static", cache_domain="funds")
def margin_trading_backup(
    trade_date: str = "",
    exchange: str = "SZSE",
    code: str = "",
    max_pages: int = 120,
) -> dict[str, Any]:
    """Exchange-official margin trading table (SSE / SZSE), normalised to 元 and 股."""
    day = require_date(trade_date or None, last_trading_day())
    target = code_or_raise(code) if code else ""
    key = str(exchange or "SZSE").strip().upper()
    if key in {"SH", "SSE", "SHSE", "上海"}:
        key = "SSE"
    elif key in {"SZ", "SZSE", "深圳"}:
        key = "SZSE"
    elif key in {"BOTH", "ALL"}:
        key = "BOTH"
    else:
        from ..core.errors import TickHubError

        raise TickHubError("QFSD-9001", f"invalid exchange: {exchange}", detail={"allowed": ["SSE", "SZSE", "BOTH"]})

    sse_part: list[dict[str, Any]] = []
    if key in {"SSE", "BOTH"}:
        sse_rows = _sse_margin_rows(day)
        if target:
            sse_rows = [r for r in sse_rows if normalize_code(r.get("stockCode")) == target]
        sse_part = [_map_sse_margin(r) for r in sse_rows]
        if key == "SSE":
            if not sse_part:
                return empty(
                    "sse",
                    f"上交所融资融券明细 {day} 无数据（非交易日或尚未披露）",
                    date=day,
                    exchange="SSE",
                )
            return ok(
                {"date": day, "exchange": "SSE", "unit": UNIT_YUAN_SHARE, "records": sse_part},
                "sse",
                extra={"count": len(sse_part)},
            )

    sz_rows = _szse_margin_rows(day, target, max(1, min(int(max_pages or 120), 200)))
    sz_part = [_map_szse_margin(r) for r in sz_rows]

    if key == "SZSE":
        if not sz_part:
            return empty(
                "szse",
                f"深交所融资融券明细 {day} 无数据：{_szse_lag_note(day)}",
                date=day,
                exchange="SZSE",
            )
        return ok(
            {
                "date": day,
                "exchange": "SZSE",
                "unit": UNIT_YUAN_SHARE,
                "source_unit": {
                    "amount": "亿元",
                    "volume": "万股",
                    "note": "深交所报表以亿元/万股披露，此处已换算为元/股",
                },
                "summary": None if target else _szse_margin_summary(day),
                "records": sz_part,
            },
            "szse",
            extra={"count": len(sz_part)},
        )

    if not sse_part and not sz_part:
        return empty("exchange", f"沪深两市融资融券明细 {day} 均无数据", date=day, exchange="BOTH")
    payload = ok(
        {"date": day, "exchange": "BOTH", "unit": UNIT_YUAN_SHARE, "sse": sse_part, "szse": sz_part},
        "exchange",
        extra={"count": len(sse_part) + len(sz_part)},
    )
    if not sz_part:
        payload["meta"]["note"] = _szse_lag_note(day)
    return payload


# -------------------------------------------------------------------- BSE quotes


def _bse_page(page: int, code: str) -> dict[str, Any]:
    form = {
        "page": page,
        "type_en": '["B"]',
        "sortfield": "hqzqdm",
        "sorttype": "asc",
        "xxfcbj_en": "[2]",
        "zqdm": code,
    }
    payload = request("exchange", "POST", _BSE_QUOTE, data=form, headers=_BSE_HEADERS)
    return payload[0] if isinstance(payload, list) and payload else {}


def _map_bse(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": str(row.get("hqzqdm") or ""),
        "name": _clean(row.get("hqzqjc")),
        "date": str(row.get("hqjsrq") or ""),
        "quote_time": str(row.get("hqgxsj") or ""),
        "open": to_float(row.get("hqjrkp")),
        "high": to_float(row.get("hqzgcj")),
        "low": to_float(row.get("hqzdcj")),
        "close": to_float(row.get("hqzjcj")),
        "previous_close": to_float(row.get("hqzrsp")),
        "change": to_float(row.get("hqzd")),
        "change_pct": to_float(row.get("hqzdf")),
        "volume": to_float(row.get("hqcjsl")),
        "amount": to_float(row.get("hqcjje")),
        "pe_source": to_float(row.get("hqsyl1")),
        "bids": [
            {"price": to_float(row.get(f"hqbjw{i}")), "volume": to_float(row.get(f"hqbsl{i}"))} for i in range(1, 6)
        ],
        "asks": [
            {"price": to_float(row.get(f"hqsjw{i}")), "volume": to_float(row.get(f"hqssl{i}"))} for i in range(1, 6)
        ],
    }


@tool(layer="quotes", ttl="semi_static", cache_domain="quotes")
def bse_quote_backup(trade_date: str = "", code: str = "", max_pages: int = 20) -> dict[str, Any]:
    """Beijing Stock Exchange closing snapshot with 5-level book (official source)."""
    target = code_or_raise(code) if code else ""
    if target and market_of(target) != "bj":
        from ..core.errors import TickHubError

        raise TickHubError("QFSD-9001", f"{code} 不是北交所证券", detail={"code": target})
    want = require_date(trade_date or None, "") if trade_date else ""

    first = _bse_page(0, target)
    rows = [r for r in first.get("content") or [] if isinstance(r, dict)]
    pages = to_int(first.get("totalPages"), 1) or 1
    for page in range(1, min(pages, max(1, min(int(max_pages or 20), 100)))):
        rows.extend(r for r in _bse_page(page, target).get("content") or [] if isinstance(r, dict))

    if not rows:
        return empty("bse", f"北交所行情快照无 {target or '数据'}", code=target or None)
    snapshot = str(rows[0].get("hqjsrq") or "")
    snapshot_date = f"{snapshot[:4]}-{snapshot[4:6]}-{snapshot[6:]}" if re.fullmatch(r"\d{8}", snapshot) else snapshot
    if want and want != snapshot_date:
        return empty(
            "bse",
            f"北交所行情接口只提供最新收盘快照（{snapshot_date}），无法回补 {want}",
            date=want,
            snapshot_date=snapshot_date,
        )
    quotes = [_map_bse(r) for r in rows]
    return ok(
        {"date": snapshot_date, "unit": UNIT_YUAN_SHARE, "count": len(quotes), "quotes": quotes},
        "bse",
        extra={"count": len(quotes)},
    )


# ------------------------------------------------------------------- dragon tiger


def _parse_sse_lhb(lines: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    section = ""
    board = ""
    for line in lines:
        text = line.rstrip()
        if not text.strip():
            continue
        head = _SSE_SECTION_RE.match(text)
        if head:
            section = head.group(2).strip()
            board = ""
            continue
        sub = _SSE_BOARD_RE.match(text)
        if sub:
            board = sub.group(1)
            continue
        match = _SSE_LHB_ROW_RE.match(text)
        if not match:
            continue
        amount_wan = to_float(match.group(5))
        out.append(
            {
                "code": match.group(1),
                "name": match.group(2),
                "amount": None if amount_wan is None else round(amount_wan * 1e4, 2),
                "volume": to_float(match.group(4).replace(",", "")),
                "reason": section,
                "period": match.group(6),
                "board": board,
                "source": "sse",
            }
        )
    return out


def _section_rows(day: str) -> list[dict[str, Any]]:
    payload = request(
        "sse",
        "GET",
        _SSE_LHB,
        params={"jsonCallBack": "cb", "isPagination": "false", "dateTx": day},
        headers={"Referer": _SSE_REFERER},
    )
    return _parse_sse_lhb([str(line) for line in (payload or {}).get("fileContents") or []])


def _szse_lhb_rows(day: str, max_pages: int) -> list[dict[str, Any]]:
    params = {"SHOWTYPE": "JSON", "CATALOGID": "1842_xxpl", "TABKEY": "tab1", "txtStart": day, "txtEnd": day}
    headers = {"Referer": _SZSE_LHB_REFERER}
    out: list[dict[str, Any]] = []
    pages = 1
    number = 1
    while number <= min(pages, max_pages):
        block = _szse_block(
            request("exchange", "GET", _SZSE_REPORT, params={**params, "PAGENO": number}, headers=headers)
        )
        if number == 1:
            pages = to_int((block.get("metadata") or {}).get("pagecount"), 1) or 1
        batch = [r for r in block.get("data") or [] if isinstance(r, dict)]
        if not batch:
            break
        for row in batch:
            detail = None
            link = _SZSE_DETAIL_RE.search(str(row.get("bz") or ""))
            if link:
                detail = "https://www.szse.cn/api/report" + link.group(1)
            out.append(
                {
                    "code": str(row.get("zqdm") or ""),
                    "name": _clean(row.get("zqjc")),
                    "amount": _yuan(row.get("cjje"), 1e8),
                    "volume": _shares(row.get("cjsl"), 1e4),
                    "reason": _clean(row.get("plyy")),
                    "period": None,
                    "board": None,
                    "detail_url": detail,
                    "source": "szse",
                }
            )
        number += 1
    return out


@tool(layer="market", ttl="semi_static", cache_domain="market")
def dragon_tiger_backup(trade_date: str = "", max_pages: int = 40) -> dict[str, Any]:
    """Exchange-official 龙虎榜 list for one trading day (SSE fixed-width + SZSE JSON)."""
    day = require_date(trade_date or None, last_trading_day())
    sse_rows = _section_rows(day)
    szse_rows = _szse_lhb_rows(day, max(1, min(int(max_pages or 40), 120)))
    records = sse_rows + szse_rows
    if not records:
        return empty("exchange", f"{day} 沪深交易所龙虎榜均未发布（非交易日或尚未披露）", date=day)
    return ok(
        {"date": day, "unit": UNIT_YUAN_SHARE, "sse": sse_rows, "szse": szse_rows, "records": records},
        "exchange",
        extra={"count": len(records)},
    )


# -------------------------------------------------------------- fund flow history


@tool(layer="funds", ttl="semi_static", cache_domain="funds")
def fund_flow_history_backup(code: str, days: int = 60) -> dict[str, Any]:
    """Sina daily capital-flow history used when Eastmoney flow history is down."""
    c = code_or_raise(code)
    from ..core.ticker import sina_symbol

    limit = max(1, min(int(days or 60), 500))
    rows = request(
        "sina",
        "GET",
        _SINA_FLOW_HISTORY,
        params={"page": 1, "num": limit, "sort": "opendate", "asc": 0, "daima": sina_symbol(c)},
        headers={"Referer": "https://finance.sina.com.cn/"},
        encoding="gbk",
    )
    if not isinstance(rows, list) or not rows:
        return empty("sina", "新浪资金流向历史不可用（北交所无覆盖或当日未更新）", code=c)
    flows = []
    for row in rows:
        change = to_float(row.get("changeratio"))
        flows.append(
            {
                "date": row.get("opendate"),
                "close": to_float(row.get("trade")),
                "change_pct": None if change is None else round(change * 100, 4),
                "turnover_pct": to_float(row.get("turnover")),
                "net_amount": to_float(row.get("netamount")),
                "net_ratio": to_float(row.get("ratioamount")),
                "super_large_net": to_float(row.get("r0_net")),
                "super_large_ratio": to_float(row.get("r0_ratio")),
            }
        )
    return ok({"code": c, "flows": flows}, "sina", source_url=_SINA_FLOW_HISTORY, extra={"count": len(flows)})


# ------------------------------------------------------------------ announcements


def _szse_announcements(code: str, page_size: int, page_index: int) -> list[dict[str, Any]]:
    import json as _json

    body = {
        "seDate": ["", ""],
        "stock": [code],
        "channelCode": ["listedNotice_disc"],
        "pageSize": max(1, min(int(page_size or 20), 100)),
        "pageNum": max(1, int(page_index or 1)),
    }
    payload = request(
        "exchange",
        "POST",
        _SZSE_ANNOUNCE,
        data=_json.dumps(body),
        headers={"Content-Type": "application/json", "Referer": "https://www.szse.cn/disclosure/listed/notice/index.html"},
    )
    out = []
    for item in (payload or {}).get("data") or []:
        path = str(item.get("attachPath") or "")
        pdf = _SZSE_DOWNLOAD + path if path else ""
        codes = item.get("secCode") or []
        names = item.get("secName") or []
        out.append(
            {
                "code": codes[0] if isinstance(codes, list) and codes else codes,
                "name": _clean(names[0] if isinstance(names, list) and names else names),
                "title": _clean(item.get("title")),
                "format": item.get("attachFormat"),
                "size_kb": item.get("attachSize"),
                "publish_time": item.get("publishTime"),
                "pdf": pdf or None,
                "url": pdf or None,
                "source": "szse",
            }
        )
    return out


def _eastmoney_announcements(code: str, page_size: int, page_index: int) -> list[dict[str, Any]]:
    payload = request(
        "eastmoney",
        "GET",
        _EM_ANNOUNCE,
        params={
            "sr": -1,
            "page_size": max(1, min(int(page_size or 20), 100)),
            "page_index": max(1, int(page_index or 1)),
            "ann_type": "A",
            "client_source": "web",
            "stock_list": code,
            "f_node": 0,
            "s_node": 0,
        },
    )
    out = []
    for item in ((payload or {}).get("data") or {}).get("list") or []:
        art = str(item.get("art_code") or "")
        holder = (item.get("codes") or [{}])[0] or {}
        columns = [c.get("column_name") for c in item.get("columns") or [] if c.get("column_name")]
        out.append(
            {
                "code": holder.get("stock_code") or code,
                "name": _clean(holder.get("short_name")),
                "title": _clean(item.get("title")),
                "type": ",".join(columns),
                "publish_time": str(item.get("notice_date") or "")[:19],
                "pdf": _EM_PDF.format(art=art) if art else None,
                "url": _EM_NOTICE.format(code=code, art=art) if art else None,
                "source": "eastmoney",
            }
        )
    return out


@tool(layer="announcements", ttl="semi_static", cache_domain="announcement")
def announcements_backup(code: str, page_size: int = 20, page_index: int = 1) -> dict[str, Any]:
    """Official-exchange announcement list with PDF links (SZSE direct, others via Eastmoney)."""
    c = code_or_raise(code)
    if market_of(c) == "sz":
        rows = _szse_announcements(c, page_size, page_index)
        source = "szse"
    else:
        rows = _eastmoney_announcements(c, page_size, page_index)
        source = "eastmoney"
    if not rows:
        return empty(source, f"{c} 公告列表为空", code=c)
    return ok({"code": c, "announcements": rows}, source, extra={"count": len(rows)})


__all__ = [
    "announcements_backup",
    "bse_quote_backup",
    "dragon_tiger_backup",
    "fund_flow_history_backup",
    "margin_trading_backup",
]
