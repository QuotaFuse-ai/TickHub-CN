"""Capital flows, margin trading, block trades, holders and dividends."""

from __future__ import annotations

import datetime as _dt
import json
import math
import re
from typing import Any

from ..core.errors import TickHubError
from ..core.http import em_datacenter, em_get, request
from ..core.ticker import eastmoney_secid, normalize_code, sina_symbol
from .base import tool
from .financials import _baostock, _bs_code
from .util import code_or_raise, empty, frame, now_shanghai, ok, require_date, to_float, to_int

_PUSH2 = "https://push2.eastmoney.com/api/qt/stock/get"
_PUSH2HIS = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
_KAMT = "https://push2.eastmoney.com/api/qt/kamt/get"
_SINA_FLOW = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssi_ssfx_flzjtj"
_SINA_FLOW_HISTORY = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs"
_SINA_BOARD_FLOW = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_bk"
_SINA_NODE = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
_FUND_GMB = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_DATACENTER_REPORT = "RPT_DMSK_TS_STOCKNEW"
_DATACENTER_COLUMNS = (
    "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,CLOSE_PRICE,CHANGE_RATE,"
    "PRIME_INFLOW,SUPERDEAL_INFLOW,SUPERDEAL_OUTFLOW,BIGDEAL_INFLOW,BIGDEAL_OUTFLOW,"
    "BUY_SUPERDEAL_RATIO,BUY_BIGDEAL_RATIO"
)
# 沪市主板/科创板 and 深市主板/创业板; B shares and 北交所 are outside the push2 scope.
_DATACENTER_MARKETS = {
    "all": ("069001001001", "069001001006", "069001002001", "069001002002"),
    "sh": ("069001001001", "069001001006"),
    "sz": ("069001002001", "069001002002"),
}

_SCOPES = {
    "all": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
    "industry": "m:90+t:2",
    "concept": "m:90+t:3",
    "sh": "m:1+t:2,m:1+t:23",
    "sz": "m:0+t:6,m:0+t:80",
}

# f164/f165 = 5-day main net/pct, f174/f175 = 10-day main net/pct.
_FLOW_FIELDS = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f164,f165,f174,f175"


def _fflow_row(line: str) -> dict[str, Any] | None:
    p = line.split(",")
    if len(p) < 6:
        return None
    return {
        "date": p[0],
        "main_net": to_float(p[1]),
        "small_net": to_float(p[2]),
        "medium_net": to_float(p[3]),
        "large_net": to_float(p[4]),
        "super_large_net": to_float(p[5]),
        "main_pct": to_float(p[6]) if len(p) > 6 else None,
        "small_pct": to_float(p[7]) if len(p) > 7 else None,
        "medium_pct": to_float(p[8]) if len(p) > 8 else None,
        "large_pct": to_float(p[9]) if len(p) > 9 else None,
        "super_large_pct": to_float(p[10]) if len(p) > 10 else None,
        "close": to_float(p[11]) if len(p) > 11 else None,
        "change_pct": to_float(p[12]) if len(p) > 12 else None,
    }


def _pct(value: Any) -> float | None:
    """Sina ratios are fractions; the tool contract reports percentages."""
    raw = to_float(value)
    return None if raw is None else round(raw * 100, 4)


@tool(layer="funds", ttl="realtime")
def stock_fund_flow(code: str, days: int = 60) -> dict[str, Any]:
    """Daily main/small/medium/large/super-large net inflow for one stock."""
    c = code_or_raise(code)
    limit = min(max(1, int(days or 60)), 1000)
    try:
        payload = em_get(
            _PUSH2HIS,
            params={
                "lmt": limit,
                "klt": 101,
                "secid": eastmoney_secid(c),
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            },
        )
    except TickHubError:
        return _sina_stock_flow(c, limit)
    data = (payload or {}).get("data") or {}
    rows = [r for r in (_fflow_row(line) for line in data.get("klines") or []) if r]
    if not rows:
        return _sina_stock_flow(c, limit)
    return ok({"code": c, "name": data.get("name"), "flows": rows}, "eastmoney", extra={"count": len(rows)})


def _sina_stock_flow(code: str, limit: int) -> dict[str, Any]:
    rows = request(
        "sina",
        "GET",
        _SINA_FLOW_HISTORY,
        params={"page": 1, "num": limit, "sort": "opendate", "asc": 0, "daima": sina_symbol(code)},
        headers={"Referer": "https://finance.sina.com.cn/"},
        encoding="gbk",
    )
    if not isinstance(rows, list) or not rows:
        return empty("sina", "新浪资金流向历史不可用", code=code)
    flows = []
    for row in rows:
        change = to_float(row.get("changeratio"))
        flows.append(
            {
                "date": row.get("opendate"),
                "main_net": to_float(row.get("netamount")),
                "small_net": None,
                "medium_net": None,
                "large_net": None,
                "super_large_net": to_float(row.get("r0_net")),
                "main_pct": _pct(row.get("ratioamount")),
                "small_pct": None,
                "medium_pct": None,
                "large_pct": None,
                "super_large_pct": _pct(row.get("r0_ratio")),
                "close": to_float(row.get("trade")),
                "change_pct": None if change is None else round(change * 100, 4),
            }
        )
    return ok(
        {"code": code, "name": None, "flows": flows},
        "sina",
        source_url=_SINA_FLOW_HISTORY,
        extra={
            "count": len(flows),
            "fallback_from": "eastmoney",
            "note": "新浪历史只提供主力与超大单口径，中小单及对应占比为 null",
        },
    )


@tool(layer="funds", ttl="realtime")
def fund_flow_ranking(scope: str = "all", page: int = 1, page_size: int = 100) -> dict[str, Any]:
    """Realtime capital-flow ranking for stocks, industries or concepts (with 5/10-day totals)."""
    key = str(scope or "all").strip().lower()
    if key not in _SCOPES:
        raise TickHubError("QFSD-9001", f"invalid scope: {scope}", detail={"allowed": sorted(_SCOPES)})
    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 100)), 500)
    try:
        payload = em_get(
            _CLIST,
            params={
                "pn": page,
                "pz": page_size,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f62",
                "fs": _SCOPES[key],
                "fields": _FLOW_FIELDS,
            },
        )
    except TickHubError as exc:
        if key in {"industry", "concept"}:
            return _sina_board_flow(key, page, page_size)
        try:
            return _datacenter_stock_flow(key, page, page_size)
        except TickHubError:
            # Keep the push2 error so the caller still gets the breaker's retry hint.
            raise exc
    data = (payload or {}).get("data") or {}
    raw = data.get("diff") or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    rows = [
        {
            "code": r.get("f12"),
            "name": r.get("f14"),
            "price": to_float(r.get("f2")),
            "change_pct": to_float(r.get("f3")),
            "main_net": to_float(r.get("f62")),
            "main_net_pct": to_float(r.get("f184")),
            "super_large_net": to_float(r.get("f66")),
            "super_large_pct": to_float(r.get("f69")),
            "large_net": to_float(r.get("f72")),
            "large_pct": to_float(r.get("f75")),
            "medium_net": to_float(r.get("f78")),
            "medium_pct": to_float(r.get("f81")),
            "small_net": to_float(r.get("f84")),
            "small_pct": to_float(r.get("f87")),
            "main_net_5d": to_float(r.get("f164")),
            "main_net_5d_pct": to_float(r.get("f165")),
            "main_net_10d": to_float(r.get("f174")),
            "main_net_10d_pct": to_float(r.get("f175")),
        }
        for r in raw
    ]
    if not rows:
        if key in {"industry", "concept"}:
            return _sina_board_flow(key, page, page_size)
        return empty("eastmoney", "fund-flow ranking unavailable", scope=key)
    out = frame(rows, "eastmoney", source_url=_CLIST, scope=key)
    out["data"] = {"scope": key, "total": to_int(data.get("total"), len(rows)), "page": page, "page_size": page_size, "items": rows}
    return out


def _datacenter_turnover(row: dict[str, Any]) -> float | None:
    """Rebuild turnover from a tier's buy amount and its share of turnover."""
    for amount_field, ratio_field in (
        ("SUPERDEAL_INFLOW", "BUY_SUPERDEAL_RATIO"),
        ("BIGDEAL_INFLOW", "BUY_BIGDEAL_RATIO"),
    ):
        amount = to_float(row.get(amount_field))
        share = to_float(row.get(ratio_field))
        if amount and amount > 0 and share and share > 0:
            return amount / share
    return None


def _datacenter_pct(net: float | None, turnover: float | None) -> float | None:
    if net is None or not turnover:
        return None
    return round(net / turnover * 100, 4)


def _datacenter_net(row: dict[str, Any], inflow_field: str, outflow_field: str) -> float | None:
    inflow = to_float(row.get(inflow_field))
    outflow = to_float(row.get(outflow_field))
    if inflow is None or outflow is None:
        return None
    return inflow - outflow


def _datacenter_stock_flow(key: str, page: int, page_size: int) -> dict[str, Any]:
    """Close-of-day stock flow ranking from Eastmoney's datacenter (non-push2 host)."""
    # The datacenter filter parser rejects whitespace inside ``in (...)``.
    markets = ",".join(f'"{code}"' for code in _DATACENTER_MARKETS[key])
    payload = em_get(
        _DATACENTER,
        params={
            "reportName": _DATACENTER_REPORT,
            "columns": _DATACENTER_COLUMNS,
            "filter": f"(TRADE_MARKET_CODE in ({markets}))",
            "pageNumber": page,
            "pageSize": page_size,
            "sortColumns": "PRIME_INFLOW",
            "sortTypes": "-1",
            "source": "WEB",
            "client": "WEB",
        },
    )
    result = (payload or {}).get("result") or {}
    raw = result.get("data") or []
    if not raw:
        return empty(
            "eastmoney",
            "push2 资金流排名不可用，备用日频库同样无数据",
            scope=key,
            fallback_from="push2",
            degraded="eod_datacenter",
        )
    rows = []
    for r in raw:
        main_net = to_float(r.get("PRIME_INFLOW"))
        super_net = _datacenter_net(r, "SUPERDEAL_INFLOW", "SUPERDEAL_OUTFLOW")
        large_net = _datacenter_net(r, "BIGDEAL_INFLOW", "BIGDEAL_OUTFLOW")
        turnover = _datacenter_turnover(r)
        rows.append(
            {
                "code": r.get("SECURITY_CODE"),
                "name": r.get("SECURITY_NAME_ABBR"),
                "price": to_float(r.get("CLOSE_PRICE")),
                "change_pct": to_float(r.get("CHANGE_RATE")),
                "main_net": main_net,
                "main_net_pct": _datacenter_pct(main_net, turnover),
                "super_large_net": super_net,
                "super_large_pct": _datacenter_pct(super_net, turnover),
                "large_net": large_net,
                "large_pct": _datacenter_pct(large_net, turnover),
                "medium_net": None,
                "medium_pct": None,
                "small_net": None,
                "small_pct": None,
                "main_net_5d": None,
                "main_net_5d_pct": None,
                "main_net_10d": None,
                "main_net_10d_pct": None,
            }
        )
    trade_date = str(raw[0].get("TRADE_DATE") or "")[:10] or None
    out = frame(
        rows,
        "eastmoney",
        source_url=_DATACENTER,
        scope=key,
        fallback_from="push2",
        degraded="eod_datacenter",
        data_as_of=trade_date,
        note="push2 熔断，改用收盘日频排名：仅主力/超大单/大单口径，中单、小单与 5/10 日累计为空",
    )
    out["data"] = {
        "scope": key,
        "total": to_int(result.get("count"), len(rows)),
        "page": page,
        "page_size": page_size,
        "items": rows,
    }
    return out


def _sina_board_flow(key: str, page: int, page_size: int) -> dict[str, Any]:
    """Board flow ranking from Sina; stock scopes use the datacenter report instead."""
    raw = request(
        "sina",
        "GET",
        _SINA_BOARD_FLOW,
        params={
            "page": page,
            "num": page_size,
            "sort": "netamount",
            "asc": 0,
            "fenlei": 0 if key == "industry" else 1,
        },
        headers={"Referer": "https://finance.sina.com.cn/"},
    )
    if not isinstance(raw, list) or not raw:
        return empty("sina", "新浪板块资金流不可用", scope=key)
    rows = [
        {
            "code": r.get("category"),
            "name": r.get("name"),
            "price": to_float(r.get("avg_price")),
            "change_pct": _pct(r.get("avg_changeratio")),
            "main_net": to_float(r.get("netamount")),
            "main_net_pct": _pct(r.get("ratioamount")),
            "super_large_net": None,
            "super_large_pct": None,
            "large_net": None,
            "large_pct": None,
            "medium_net": None,
            "medium_pct": None,
            "small_net": None,
            "small_pct": None,
            "main_net_5d": None,
            "main_net_5d_pct": None,
            "main_net_10d": None,
            "main_net_10d_pct": None,
            "inflow": to_float(r.get("inamount")),
            "outflow": to_float(r.get("outamount")),
            "leader_code": r.get("ts_symbol"),
            "leader_name": r.get("ts_name"),
        }
        for r in raw
        if isinstance(r, dict)
    ]
    if not rows:
        return empty("sina", "新浪板块资金流不可用", scope=key)
    out = frame(
        rows,
        "sina",
        source_url=_SINA_BOARD_FLOW,
        scope=key,
        fallback_from="eastmoney",
        note="新浪板块代码与东财 BK 代码不同；仅提供主力净流入，四档拆分与 5/10 日累计为 null",
    )
    out["data"] = {"scope": key, "total": None, "page": page, "page_size": page_size, "items": rows}
    return out


@tool(layer="funds", ttl="realtime")
def board_fund_flow(board_type: str = "industry", page: int = 1, page_size: int = 100) -> dict[str, Any]:
    """Industry/concept board fund-flow ranking (with 5/10-day totals)."""
    key = str(board_type or "industry").strip().lower()
    if key not in {"industry", "concept"}:
        raise TickHubError("QFSD-9001", "board_type must be 'industry' or 'concept'")
    return fund_flow_ranking(scope=key, page=page, page_size=page_size)


@tool(layer="funds", ttl="realtime")
def realtime_fund_flow(code: str) -> dict[str, Any]:
    """Realtime snapshot of one stock's main-capital flow."""
    c = code_or_raise(code)
    try:
        payload = em_get(
            _PUSH2,
            params={
                "secid": eastmoney_secid(c),
                "fields": "f57,f58,f43,f62,f66,f69,f72,f75,f78,f81,f84,f87,f184",
            },
        )
    except TickHubError:
        return _sina_realtime_flow(c)
    data = (payload or {}).get("data") or {}
    if not data:
        return _sina_realtime_flow(c)
    return ok(
        {
            "code": data.get("f57") or c,
            "name": data.get("f58"),
            "price": to_float(data.get("f43")),
            "main_net": to_float(data.get("f62")),
            "main_net_pct": to_float(data.get("f184")),
            "super_large_net": to_float(data.get("f66")),
            "large_net": to_float(data.get("f72")),
            "medium_net": to_float(data.get("f78")),
            "small_net": to_float(data.get("f84")),
        },
        "eastmoney",
    )


def _sina_realtime_flow(code: str) -> dict[str, Any]:
    text = request("sina", "GET", _SINA_FLOW, params={"daima": sina_symbol(code)}, expect_json=False)
    text = text.strip().rstrip(";")
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return empty("sina", "新浪实时资金流不可用", code=code)
    row = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(row, dict) or not row:
        return empty("sina", "新浪实时资金流不可用", code=code)

    def _net(prefix: str) -> float | None:
        inflow, outflow = to_float(row.get(prefix + "_in")), to_float(row.get(prefix + "_out"))
        return None if inflow is None or outflow is None else round(inflow - outflow, 2)

    change = to_float(row.get("changeratio"))
    return ok(
        {
            "code": code,
            "name": row.get("name"),
            "price": to_float(row.get("trade")),
            "main_net": to_float(row.get("netamount")),
            "main_net_pct": None,
            "super_large_net": _net("r0"),
            "large_net": _net("r1"),
            "medium_net": _net("r2"),
            "small_net": _net("r3"),
            "change_pct": None if change is None else round(change * 100, 4),
        },
        "sina",
        source_url=_SINA_FLOW,
        extra={
            "fallback_from": "eastmoney",
            "note": "新浪实时口径不提供各档占比，pct 字段为 null",
        },
    )


@tool(layer="funds", ttl="realtime")
def fund_flow_backup(code: str) -> dict[str, Any]:
    """Sina backup for one stock's realtime money flow."""
    c = code_or_raise(code)
    text = request("sina", "GET", _SINA_FLOW, params={"daima": sina_symbol(c)}, expect_json=False)
    text = text.strip().rstrip(";")
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return empty("sina", "sina money flow unavailable", code=c)
    if not payload:
        return empty("sina", "sina money flow empty", code=c)
    row = payload[0] if isinstance(payload, list) else payload

    def _pair(prefix: str) -> tuple[float | None, float | None]:
        return to_float(str(row.get(prefix + "_in", "")).replace("%", "")), to_float(
            str(row.get(prefix + "_out", "")).replace("%", "")
        )

    out = {"code": c, "date": row.get("opendate") or row.get("date")}
    for prefix, label in (("r0", "super_large"), ("r1", "large"), ("r2", "medium"), ("r3", "small")):
        inflow, outflow = _pair(prefix)
        out[label + "_in"] = inflow
        out[label + "_out"] = outflow
        out[label + "_net"] = None if inflow is None or outflow is None else round(inflow - outflow, 2)
    return ok(out, "sina", source_url=_SINA_FLOW)


_NORTHBOUND_CHANNELS = {"001": ("hk2sh", "沪股通"), "003": ("hk2sz", "深股通")}
_NORTHBOUND_HISTORY_DAYS = 250


def _northbound_history(days: int) -> dict[str, Any]:
    """Daily northbound turnover from Eastmoney's datacenter report.

    The exchange stopped publishing the daily *net buy* (``NET_DEAL_AMT``)
    after 2024-08-16 — every later row carries null. Turnover (``DEAL_AMT``)
    and trade count (``DEAL_NUM``) are still updated every session, so those
    are the honest day-frequency series; net inflow stays ``null`` instead of
    being invented as zero. Upstream amounts are in 百万元 and are converted
    to 万元 here so they line up with the realtime channels.
    """
    span = max(1, min(int(days or 60), _NORTHBOUND_HISTORY_DAYS))
    rows = em_datacenter(
        "RPT_MUTUAL_DEAL_HISTORY",
        columns=(
            "MUTUAL_TYPE,TRADE_DATE,DEAL_AMT,DEAL_NUM,NET_DEAL_AMT,"
            "QUOTA_BALANCE_TEXT,LEAD_STOCKS_NAME,LS_CHANGE_RATE,"
            "INDEX_CLOSE_PRICE,INDEX_CHANGE_RATE"
        ),
        filter_str='(MUTUAL_TYPE in ("001","003"))',
        page_size=span * 2,
        sort_columns="TRADE_DATE",
        sort_types="-1",
    )
    out: list[dict[str, Any]] = []
    for row in rows or []:
        channel = _NORTHBOUND_CHANNELS.get(str(row.get("MUTUAL_TYPE") or ""))
        if channel is None:
            continue
        net = to_float(row.get("NET_DEAL_AMT"))
        deal = to_float(row.get("DEAL_AMT"))
        out.append(
            {
                "trade_date": str(row.get("TRADE_DATE") or "")[:10],
                "channel": channel[0],
                "name": channel[1],
                "deal_amount_wan": None if deal is None else round(deal * 100, 2),
                "deal_num": to_int(row.get("DEAL_NUM")),
                "net_inflow": None if net is None else round(net * 100, 2),
                "quota_text": str(row.get("QUOTA_BALANCE_TEXT") or ""),
                "lead_stock": row.get("LEAD_STOCKS_NAME"),
                "lead_change_pct": to_float(row.get("LS_CHANGE_RATE")),
                "index_close": to_float(row.get("INDEX_CLOSE_PRICE")),
                "index_change_pct": to_float(row.get("INDEX_CHANGE_RATE")),
            }
        )
    if not out:
        return empty("eastmoney", "northbound history unavailable")
    latest = max(str(r["trade_date"]) for r in out)
    net_published = str(latest) < "2024-08-19"
    return ok(
        {"days": out, "latest": latest},
        "eastmoney",
        extra={
            "count": len(out),
            "unit": "万元",
            "net_inflow_published": net_published,
            "note": (
                "DEAL_AMT 为当日成交额（万元），逐日更新。"
                "NET_DEAL_AMT（当日净买额）自 2024-08-19 起上游停止披露，此后为 null，未以 0 填充。"
            ),
        },
    )


@tool(layer="funds", ttl="realtime")
def northbound_flow(history: bool = False, days: int = 60) -> dict[str, Any]:
    """Northbound / southbound intraday net flow; ``history=True`` gives daily turnover history."""
    if history:
        return _northbound_history(days)
    payload = em_get(_KAMT, params={"fields1": "f1,f2,f3,f4", "fields2": "f51,f52,f53,f54"})
    data = (payload or {}).get("data") or {}
    if not data:
        return empty("eastmoney", "northbound data unavailable")
    channels = {
        "hk2sh": "沪股通",
        "hk2sz": "深股通",
        "sh2hk": "港股通(沪)",
        "sz2hk": "港股通(深)",
    }
    rows = []
    active = False
    closed = False
    for key, label in channels.items():
        block = data.get(key) or {}
        net = to_float(block.get("dayNetAmtIn"))
        quota = to_float(block.get("dayAmtThreshold"))
        status = to_int(block.get("status"))
        # Outside the session a channel echoes its whole daily quota back as
        # ``dayNetAmtIn``; that number is a placeholder, not money that moved.
        placeholder = bool(quota) and net == quota
        if net and not placeholder:
            active = True
        closed = closed or status == 3
        rows.append(
            {
                "channel": key,
                "name": label,
                "net_inflow": 0.0 if placeholder else net,
                "net_inflow_wan": to_float(block.get("dayAmtIn")),
                "status": status,
            }
        )
    if not active:
        return empty(
            "eastmoney",
            "northbound closed or not yet published",
            channels=rows,
            closed=closed,
            unit="万元",
        )
    return ok(
        {"channels": rows},
        "eastmoney",
        extra={"count": len(rows), "closed": closed, "unit": "万元"},
    )


@tool(layer="funds", ttl="semi_static")
def margin_detail(code: str, page_size: int = 100) -> dict[str, Any]:
    """Per-stock margin financing (融资融券) history."""
    c = code_or_raise(code)
    size = min(max(1, int(page_size or 100)), 1000)
    rows = em_datacenter(
        "RPTA_WEB_RZRQ_GGMX",
        filter_str=f'(SCODE="{c}")',
        page_size=size,
        sort_columns="DATE",
        sort_types="-1",
    )
    if not rows:
        return empty("eastmoney", "margin detail empty", code=c)
    out = []
    for r in rows:
        out.append(
            {
                "date": str(r.get("DATE") or "")[:10],
                "market": r.get("MARKET"),
                "code": r.get("SCODE"),
                "name": r.get("SECNAME"),
                "financing_balance": to_float(r.get("RZYE")),
                "financing_buy": to_float(r.get("RZMRE")),
                "financing_repay": to_float(r.get("RZCHE")),
                "securities_balance": to_float(r.get("RQYE")),
                "securities_sell": to_float(r.get("RQMCL")),
                "securities_repay": to_float(r.get("RQCHL")),
                "total_balance": to_float(r.get("RZRQYE")),
            }
        )
    return ok({"code": c, "records": out}, "eastmoney", extra={"count": len(out)})


@tool(layer="funds", ttl="semi_static")
def margin_market(days: int = 60) -> dict[str, Any]:
    """Market-wide margin balance history."""
    size = min(max(1, int(days or 60)), 500)
    rows = em_datacenter(
        "RPTA_RZRQ_LSHJ",
        page_size=size,
        sort_columns="DIM_DATE",
        sort_types="-1",
    )
    if not rows:
        return empty("eastmoney", "margin market history unavailable")
    out = [
        {
            "date": str(r.get("DIM_DATE") or "")[:10],
            "financing_balance": to_float(r.get("RZYE")),
            "securities_balance": to_float(r.get("RQYE")),
            "total_balance": to_float(r.get("RZRQYE")),
            "financing_buy": to_float(r.get("RZMRE")),
            "total_market_cap": to_float(r.get("LTSZ")),
            "balance_ratio": to_float(r.get("RZRQYECZ")),
        }
        for r in rows
    ]
    return frame(out, "eastmoney", source_url="datacenter/RPTA_RZRQ_LSHJ")


@tool(layer="funds", ttl="semi_static")
def block_trades(code: str = "", date: str = "", page_size: int = 100) -> dict[str, Any]:
    """Block-trade (大宗交易) records by stock or trading date."""
    size = min(max(1, int(page_size or 100)), 500)
    filters = []
    if code:
        filters.append(f'(SECURITY_CODE="{code_or_raise(code)}")')
    if date:
        filters.append(f"(TRADE_DATE='{require_date(date)}')")
    rows = em_datacenter(
        "RPT_DATA_BLOCKTRADE",
        filter_str="".join(filters),
        page_size=size,
        sort_columns="TRADE_DATE",
        sort_types="-1",
    )
    if not rows:
        return empty("eastmoney", "block trades empty", code=code or None, date=date or None)
    out = []
    for r in rows:
        out.append(
            {
                "date": str(r.get("TRADE_DATE") or "")[:10],
                "code": r.get("SECURITY_CODE"),
                "name": r.get("SECURITY_NAME_ABBR"),
                "price": to_float(r.get("DEAL_PRICE") or r.get("PRICE")),
                "volume": to_float(r.get("DEAL_VOLUME") or r.get("VOLUME")),
                "amount": to_float(r.get("DEAL_AMT") or r.get("AMOUNT")),
                "premium_rate": to_float(r.get("PREMIUM_RATIO")),
                "buyer": r.get("BUYER_NAME"),
                "seller": r.get("SELLER_NAME"),
            }
        )
    return frame(out, "eastmoney", source_url="datacenter/RPT_DATA_BLOCKTRADE")


@tool(layer="funds", ttl="semi_static")
def holder_number(code: str) -> dict[str, Any]:
    """Latest shareholder-count snapshot (股东户数) for one stock."""
    c = code_or_raise(code)
    rows = em_datacenter(
        "RPT_HOLDERNUMLATEST",
        filter_str=f'(SECURITY_CODE="{c}")',
        page_size=10,
        sort_columns="END_DATE",
        sort_types="-1",
    )
    if not rows:
        return empty("eastmoney", "holder number unavailable", code=c)
    out = [
        {
            "code": r.get("SECURITY_CODE"),
            "name": r.get("SECURITY_NAME_ABBR"),
            "end_date": str(r.get("END_DATE") or r.get("REPORT_DATE") or "")[:10],
            "holder_num": to_int(r.get("HOLDER_NUM")),
            "holder_num_change": to_int(r.get("HOLDER_NUM_CHANGE")),
            "holder_num_change_pct": to_float(r.get("HOLDER_NUM_RATIO")),
            "avg_market_cap": to_float(r.get("AVG_MARKET_CAP")),
            "avg_holding": to_float(r.get("AVG_HOLD_NUM")),
        }
        for r in rows
    ]
    return ok({"code": c, "records": out}, "eastmoney", extra={"count": len(out)})


@tool(layer="funds", ttl="semi_static")
def dividend_history(code: str) -> dict[str, Any]:
    """Dividend / bonus-share history (分红送转) for one stock."""
    c = code_or_raise(code)
    rows = em_datacenter(
        "RPT_SHAREBONUS_DET",
        filter_str=f'(SECURITY_CODE="{c}")',
        page_size=100,
        sort_columns="REPORT_DATE",
        sort_types="-1",
    )
    if not rows:
        return empty("eastmoney", "dividend history empty", code=c)
    out = []
    for r in rows:
        out.append(
            {
                "report_date": str(r.get("REPORT_DATE") or "")[:10],
                "ex_date": str(r.get("EX_DIVIDEND_DATE") or "")[:10] or None,
                "record_date": str(r.get("EQUITY_RECORD_DATE") or "")[:10] or None,
                "plan": r.get("IMPL_PLAN_PROFILE") or r.get("PLAN_PROFILE"),
                "dividend_per_share": to_float(r.get("PRETAX_BONUS_RMB")),
                "bonus_ratio": to_float(r.get("BONUS_IT_RATIO")),
                "transfer_ratio": to_float(r.get("TRANSFER_IT_RATIO")),
                "dividend_yield": to_float(r.get("DIVIDENT_RATIO")),
            }
        )
    return frame(out, "eastmoney", source_url="datacenter/RPT_SHAREBONUS_DET")


@tool(layer="funds", ttl="semi_static")
def etf_quotes(page: int = 1, page_size: int = 100) -> dict[str, Any]:
    """ETF market snapshot (price, turnover, market cap)."""
    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 100)), 500)
    try:
        payload = em_get(
            _CLIST,
            params={
                "pn": page,
                "pz": page_size,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024",
                "fields": "f12,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f20,f21",
            },
        )
    except TickHubError:
        return _sina_etf_quotes(page, page_size)
    data = (payload or {}).get("data") or {}
    raw = data.get("diff") or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    rows = [
        {
            "code": r.get("f12"),
            "name": r.get("f14"),
            "price": to_float(r.get("f2")),
            "change_pct": to_float(r.get("f3")),
            "change": to_float(r.get("f4")),
            "volume": to_int(r.get("f5")),
            "amount": to_float(r.get("f6")),
            "high": to_float(r.get("f15")),
            "low": to_float(r.get("f16")),
            "open": to_float(r.get("f17")),
            "prev_close": to_float(r.get("f18")),
            "total_market_cap": to_float(r.get("f20")),
            "float_market_cap": to_float(r.get("f21")),
        }
        for r in raw
    ]
    if not rows:
        return _sina_etf_quotes(page, page_size)
    out = frame(rows, "eastmoney", source_url=_CLIST)
    out["data"] = {"total": to_int(data.get("total"), len(rows)), "page": page, "page_size": page_size, "items": rows}
    return out


def _sina_etf_quotes(page: int, page_size: int) -> dict[str, Any]:
    raw = request(
        "sina",
        "GET",
        _SINA_NODE,
        params={"page": page, "num": page_size, "sort": "changepercent", "asc": 0, "node": "etf_hq_fund"},
        headers={"Referer": "https://finance.sina.com.cn/"},
    )
    if not isinstance(raw, list):
        raw = []
    rows = [
        {
            "code": r.get("code"),
            "name": r.get("name"),
            "price": to_float(r.get("trade")),
            "change_pct": to_float(r.get("changepercent")),
            "change": to_float(r.get("pricechange")),
            "volume": to_int(r.get("volume")),
            "amount": to_float(r.get("amount")),
            "high": to_float(r.get("high")),
            "low": to_float(r.get("low")),
            "open": to_float(r.get("open")),
            "prev_close": to_float(r.get("settlement")),
            "total_market_cap": _wan_to_yuan(r.get("mktcap")),
            "float_market_cap": _wan_to_yuan(r.get("nmc")),
        }
        for r in raw
        if isinstance(r, dict)
    ]
    if not rows:
        return empty("sina", "ETF snapshot unavailable")
    return ok(
        {"total": None, "page": page, "page_size": page_size, "items": rows},
        "sina",
        extra={"count": len(rows), "fallback_from": "eastmoney", "amount_unit": "yuan", "market_cap_unit": "yuan"},
    )


def _wan_to_yuan(value: Any) -> float | None:
    num = to_float(value)
    return round(num * 10000, 2) if num else None


_TABLE_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TABLE_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


@tool(layer="funds", ttl="semi_static")
def etf_share_history(code: str, page_size: int = 40) -> dict[str, Any]:
    """ETF share-scale history parsed from Eastmoney fund archives."""
    c = code_or_raise(code)
    text = request(
        "eastmoney",
        "GET",
        _FUND_GMB,
        params={"type": "gmbd", "code": c},
        headers={"Referer": "https://fundf10.eastmoney.com/"},
        expect_json=False,
        encoding="utf-8",
    )
    rows = []
    for chunk in _TABLE_ROW_RE.findall(text):
        cells = [_TAG_RE.sub("", c).strip().replace("&nbsp;", "") for c in _TABLE_CELL_RE.findall(chunk)]
        if len(cells) < 2 or not re.match(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue
        rows.append({"date": cells[0], "shares": to_float(cells[1]), "raw": cells[1:]})
    rows = rows[: min(max(1, int(page_size or 40)), 200)]
    if not rows:
        return empty("eastmoney", "ETF share history unavailable", code=c)
    return ok({"code": c, "records": rows}, "eastmoney", extra={"count": len(rows)})


def _triangular_weights(grid: list[float], low: float, high: float, avg: float) -> list[float]:
    """Triangular kernel peaking at ``avg``, normalised to unit mass."""
    n = len(grid)
    w = [0.0] * n
    if not (math.isfinite(low) and math.isfinite(high) and math.isfinite(avg)) or high < low:
        return w
    if high - low < 1e-9:
        w[min(range(n), key=lambda i: abs(grid[i] - low))] = 1.0
        return w
    avg = min(max(avg, low), high)
    for i, p in enumerate(grid):
        if low <= p <= avg:
            w[i] = 1.0 if avg - low < 1e-9 else (p - low) / (avg - low)
        elif avg < p <= high:
            w[i] = 1.0 if high - avg < 1e-9 else (high - p) / (high - avg)
    total = sum(w)
    if total > 0:
        return [x / total for x in w]
    # A band narrower than one grid step can miss every node; dropping the day
    # would also drop its turnover decay, so pin it to the nearest node instead.
    w[min(range(n), key=lambda i: abs(grid[i] - avg))] = 1.0
    return w


def _price_at(q: float, cum: list[float], grid: list[float]) -> float:
    for i, c in enumerate(cum):
        if c >= q:
            if i == 0 or c <= cum[i - 1]:
                return grid[i]
            return grid[i - 1] + (grid[i] - grid[i - 1]) * (q - cum[i - 1]) / (c - cum[i - 1])
    return grid[-1]


@tool(layer="funds", ttl="realtime")
def chip_distribution(code: str, days: int = 250, grid_size: int = 300, decay: float = 1.0) -> dict[str, Any]:
    """Chip distribution (筹码分布) simulated locally from OHLC + turnover.

    Eastmoney has no public CYQ endpoint, so each day's turnover decays the
    existing chips and seeds a triangular distribution over [low, high] peaking
    at the day's average price. ``days`` counts trading days, ``decay`` scales
    turnover (1.0 = real; 1.5–2.0 mirrors THS's faster decay). Prices are
    forward-adjusted. The shape is indicative, not actual broker holdings.
    """
    bs = _baostock()
    c = _bs_code(code)
    window = min(max(20, int(days or 250)), 1000)
    nodes = min(max(50, int(grid_size or 300)), 2000)
    factor = min(max(float(decay or 1.0), 0.1), 5.0)
    today = now_shanghai().date()
    start = (today - _dt.timedelta(days=int(window * 1.7) + 10)).isoformat()

    lg = bs.login()
    if lg.error_code != "0":
        raise TickHubError("QFSD-9004", f"baostock login failed: {lg.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            c,
            "date,high,low,close,turn,tradestatus",
            start_date=start,
            end_date=today.isoformat(),
            frequency="d",
            adjustflag="2",
        )
        bars: list[tuple[str, float, float, float, float]] = []
        error: str | None = None
        while rs.error_code == "0" and rs.next():
            date, high, low, close, turn, status = rs.get_row_data()
            if status != "1":
                continue
            h, l, cl, t = to_float(high), to_float(low), to_float(close), to_float(turn)
            if h is None or l is None or cl is None or t is None or h <= 0 or l <= 0:
                continue
            bars.append((date, h, l, cl, t))
        if rs.error_code != "0":
            error = f"{rs.error_code}: {rs.error_msg}"
    finally:
        bs.logout()
    if error:
        raise TickHubError("QFSD-9003", f"baostock 行情查询失败 {error}")
    bars = bars[-window:]
    if not bars:
        raise TickHubError("QFSD-1002", f"{code} 窗口内没有有效交易数据（停牌或代码错误）")

    lo = min(bar[2] for bar in bars)
    hi = max(bar[1] for bar in bars)
    pad = (hi - lo) * 0.02 or max(lo * 0.02, 0.01)
    step = (hi - lo + 2 * pad) / (nodes - 1)
    grid = [lo - pad + i * step for i in range(nodes)]

    chips: list[float] | None = None
    for _, high, low, close, turn in bars:
        t = min(max(turn / 100.0 * factor, 0.0), 1.0)
        avg = (high + low + close) / 3.0
        w = _triangular_weights(grid, low, high, avg)
        if not any(w):
            continue
        if chips is None:
            # Seeding with the first day's full float keeps pre-window holdings
            # represented; starting from zero would normalise a few low-turnover
            # days into a 100% profile and badly distort costs.
            chips = w
            continue
        chips = [old * (1.0 - t) + new * t for old, new in zip(chips, w)]
    if chips is None:
        raise TickHubError("QFSD-1002", f"{code} 价格区间无效，无法构建筹码分布")
    total = sum(chips)
    if total <= 0:
        raise TickHubError("QFSD-1002", f"{code} 筹码总量为 0")
    chips = [x / total for x in chips]

    cum: list[float] = []
    running = 0.0
    for x in chips:
        running += x
        cum.append(running)
    p05, p15, p85, p95 = (_price_at(q, cum, grid) for q in (0.05, 0.15, 0.85, 0.95))
    price = bars[-1][3]
    return ok(
        {
            "code": c,
            "price": round(price, 4),
            "profit_ratio": round(sum(x for p, x in zip(grid, chips) if p <= price), 6),
            "avg_cost": round(sum(p * x for p, x in zip(grid, chips)), 4),
            "cost_90": [round(p05, 4), round(p95, 4)],
            "cost_70": [round(p15, 4), round(p85, 4)],
            "concentration_90": round((p95 - p05) / (p95 + p05), 6) if p95 + p05 else None,
            "concentration_70": round((p85 - p15) / (p85 + p15), 6) if p85 + p15 else None,
            "peak_price": round(grid[chips.index(max(chips))], 4),
            "histogram": [[round(p, 4), round(x, 6)] for p, x in zip(grid, chips) if x > 1e-6],
            "days": len(bars),
            "decay": factor,
        },
        "baostock",
        extra={"count": len(bars), "model": "triangular", "adjust": "qfq"},
    )
