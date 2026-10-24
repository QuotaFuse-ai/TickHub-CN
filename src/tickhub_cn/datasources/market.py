"""Market-wide signals: boards, limit pools, dragon-tiger, hot rank, anomalies."""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from ..core.errors import TickHubError
from ..core.http import em_datacenter, em_get, request
from ..core.ticker import compact_date, eastmoney_secid, market_of, normalize_code
from .base import tool
from .emdc import DATACENTER, strict_rows as _strict_rows
from .util import empty, frame, last_trading_day, now_shanghai, ok, require_date, stock_code, to_float, to_int

_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
_PUSH2 = "https://push2.eastmoney.com/api/qt/stock/get"
_PUSH2HIS = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_PUSH2EX = "https://push2ex.eastmoney.com"
_SLIST = "https://push2.eastmoney.com/api/qt/slist/get"
_THS_HOT = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
_THS_ZT = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
_EM_RANK = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
_MONITOR = "https://mobappconfig.securities.eastmoney.com/emcfg/stock_monitor.json"
_ANOMALY = "https://dycalchis.eastmoney.com/price-anomaly"

# The H5 anomaly endpoints reject anything without the fixed team/product pair.
_HQ_PARAMS = {
    "team": "h5",
    "product": "EastMoney",
    "client": "WAP",
    "version": "9001",
    "name": "WAP",
    "user": "123",
}

_ANOMALY_RULES = {
    1: "主板连续10个交易日内4次出现同向异常波动",
    2: "创业板连续10个交易日内3次出现同向异常波动",
    3: "科创板连续10个交易日内3次出现同向异常波动",
    4: "连续十个交易日内日收盘价涨跌幅偏离值累计达到+100%",
    5: "连续十个交易日内日收盘价涨跌幅偏离值累计达到-50%",
    6: "连续三十个交易日内日收盘价涨跌幅偏离值累计达到+200%",
    7: "连续三十个交易日内日收盘价涨跌幅偏离值累计达到-70%",
    8: "北交所连续10个交易日内3次出现同向异常波动",
    40: "连续十个交易日内日收盘价涨跌幅偏离值累计达到+150%",
    50: "连续十个交易日内日收盘价涨跌幅偏离值累计达到-60%",
    60: "连续30个交易日内日收盘价涨跌幅偏离值累计达到+300%",
    70: "连续30个交易日内日收盘价涨跌幅偏离值累计达到-75%",
}

# MARKET is three-valued and includes the letter "B" (BSE), not just 0/1.
_MONITOR_MARKET = {"1": "SH", "0": "SZ", "B": "BJ"}

# Vendor-internal field ids for the THS limit-up reveal; copy verbatim.
_THS_ZT_FIELDS = "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004"

_BOARD_FIELDS = "f12,f14,f2,f3,f4,f8,f62,f66,f104,f105,f128,f136,f140,f141,f207,f208,f222"
_MEMBER_FIELDS = "f12,f14,f2,f3,f4,f5,f6,f8,f9,f10,f20,f21,f23,f62,f184"

_TOPIC_POOLS = {
    "limit_up": ("getTopicZTPool", "涨停板", "fbt:asc"),
    "broken": ("getTopicZBPool", "炸板", "fbt:asc"),
    "limit_down": ("getTopicDTPool", "跌停板", "zdp:asc"),
    "strong": ("getTopicQSPool", "强势股", "zdp:desc"),
    "sub_new": ("getTopicCXPool", "次新股", "zdp:desc"),
}

_CHANGE_TYPES = {
    8201: "火箭发射",
    8202: "快速反弹",
    8203: "高台跳水",
    8194: "大笔买入",
    8204: "大笔卖出",
    8213: "封涨停板",
    8215: "打开涨停板",
    8216: "封跌停板",
    8217: "打开跌停板",
    8219: "竞价上涨",
    8220: "竞价下跌",
    8221: "向上缺口",
    8222: "向下缺口",
    8223: "60日新高",
    8225: "60日新低",
    8227: "60日大幅上涨",
    8228: "60日大幅下跌",
}


def _diff(payload: dict[str, Any] | None) -> tuple[list[dict[str, Any]], int]:
    data = ((payload or {}).get("data") or {})
    rows = data.get("diff") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    return rows, to_int(data.get("total"), len(rows)) or 0


def _clist(fs: str, fields: str, *, fid: str = "f3", page: int = 1, page_size: int = 100) -> dict[str, Any]:
    payload = em_get(
        _CLIST,
        params={
            "pn": page,
            "pz": page_size,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": fid,
            "fs": fs,
            "fields": fields,
        },
    )
    rows, total = _diff(payload)
    return {"items": rows, "total": total}


def _board_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "board_code": row.get("f12"),
        "board_name": row.get("f14"),
        "price": to_float(row.get("f2")),
        "change_pct": to_float(row.get("f3")),
        "change": to_float(row.get("f4")),
        "turnover_rate": to_float(row.get("f8")),
        "main_net_inflow": to_float(row.get("f62")),
        "main_net_ratio": to_float(row.get("f66")),
        "up_count": to_int(row.get("f104")),
        "down_count": to_int(row.get("f105")),
        "leader_name": row.get("f128"),
        "leader_change_pct": to_float(row.get("f136")),
        "leader_code": row.get("f140"),
    }


@tool(layer="market", ttl="realtime")
def industry_boards(page: int = 1, page_size: int = 100) -> dict[str, Any]:
    """Industry board ranking (东方财富行业板块) with main capital flow."""
    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 100)), 500)
    payload = _clist("m:90+t:2+f:!50", _BOARD_FIELDS, page=page, page_size=page_size)
    rows = [_board_row(r) for r in payload["items"]]
    out = frame(rows, "eastmoney", source_url=_CLIST)
    out["data"] = {"total": payload["total"], "page": page, "page_size": page_size, "items": rows}
    return out


@tool(layer="market", ttl="realtime")
def concept_boards(page: int = 1, page_size: int = 100) -> dict[str, Any]:
    """Concept board ranking (东方财富概念板块)."""
    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 100)), 500)
    payload = _clist("m:90+t:3+f:!50", _BOARD_FIELDS, page=page, page_size=page_size)
    rows = [_board_row(r) for r in payload["items"]]
    out = frame(rows, "eastmoney", source_url=_CLIST)
    out["data"] = {"total": payload["total"], "page": page, "page_size": page_size, "items": rows}
    return out


@tool(layer="market", ttl="realtime")
def board_members(board_code: str, page: int = 1, page_size: int = 100) -> dict[str, Any]:
    """Constituents of an Eastmoney industry/concept board (e.g. BK0475)."""
    code = str(board_code or "").strip().upper()
    if not code.startswith("BK"):
        raise TickHubError("QFSD-9001", f"invalid board code: {board_code} (expected BKxxxx)")
    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 100)), 500)
    payload = _clist(f"b:{code}+f:!50", _MEMBER_FIELDS, page=page, page_size=page_size)
    rows = [
        {
            "code": r.get("f12"),
            "name": r.get("f14"),
            "price": to_float(r.get("f2")),
            "change_pct": to_float(r.get("f3")),
            "change": to_float(r.get("f4")),
            "volume": to_int(r.get("f5")),
            "amount": to_float(r.get("f6")),
            "turnover_rate": to_float(r.get("f8")),
            "pe": to_float(r.get("f9")),
            "pb": to_float(r.get("f23")),
            "float_market_cap": to_float(r.get("f20")),
            "total_market_cap": to_float(r.get("f21")),
            "main_net_inflow": to_float(r.get("f62")),
            "main_net_ratio": to_float(r.get("f184")),
        }
        for r in payload["items"]
    ]
    out = frame(rows, "eastmoney", source_url=_CLIST)
    out["data"] = {"board_code": code, "total": payload["total"], "page": page, "page_size": page_size, "items": rows}
    return out


@tool(layer="market", ttl="realtime")
def board_history(board_code: str, days: int = 60) -> dict[str, Any]:
    """Daily K-line for an Eastmoney industry/concept board."""
    code = str(board_code or "").strip().upper()
    if not code.startswith("BK"):
        raise TickHubError("QFSD-9001", f"invalid board code: {board_code} (expected BKxxxx)")
    limit = min(max(1, int(days or 60)), 1000)
    payload = em_get(
        _PUSH2HIS,
        params={
            "secid": f"90.{code}",
            "klt": 101,
            "fqt": 1,
            "lmt": limit,
            "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
    )
    data = (payload or {}).get("data") or {}
    rows = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 6:
            continue
        rows.append(
            {
                "date": p[0],
                "open": to_float(p[1]),
                "close": to_float(p[2]),
                "high": to_float(p[3]),
                "low": to_float(p[4]),
                "volume": to_int(p[5]),
                "amount": to_float(p[6]) if len(p) > 6 else None,
                "amplitude": to_float(p[7]) if len(p) > 7 else None,
                "change_pct": to_float(p[8]) if len(p) > 8 else None,
                "change": to_float(p[9]) if len(p) > 9 else None,
                "turnover_rate": to_float(p[10]) if len(p) > 10 else None,
            }
        )
    if not rows:
        return empty("eastmoney", "board history unavailable", board_code=code)
    return frame(rows, "eastmoney", source_url=_PUSH2HIS, board_code=code)


@tool(layer="market", ttl="realtime")
def market_overview() -> dict[str, Any]:
    """Headline index levels plus up/down/flat breadth for the Shanghai composite."""
    targets = [
        ("1.000001", "上证指数", "sh"),
        ("0.399001", "深证成指", "sz"),
        ("0.399006", "创业板指", "sz"),
        ("1.000688", "科创50", "sh"),
    ]
    rows = []
    try:
        for secid, label, market in targets:
            payload = em_get(
                _PUSH2,
                params={"secid": secid, "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f169,f170,f104,f105,f106"},
            )
            data = (payload or {}).get("data") or {}
            if not data:
                continue
            row = {
                "code": data.get("f57"),
                "name": data.get("f58") or label,
                "market": market,
                "price": to_float(data.get("f43")),
                "high": to_float(data.get("f44")),
                "low": to_float(data.get("f45")),
                "open": to_float(data.get("f46")),
                "volume": to_int(data.get("f47")),
                "amount": to_float(data.get("f48")),
                "change": to_float(data.get("f169")),
                "change_pct": to_float(data.get("f170")),
            }
            if label == "上证指数":
                row["up_count"] = to_int(data.get("f104"))
                row["down_count"] = to_int(data.get("f105"))
                row["flat_count"] = to_int(data.get("f106"))
            rows.append(row)
    except TickHubError:
        return _tencent_overview()
    if not rows:
        return _tencent_overview()
    return frame(rows, "eastmoney", source_url=_PUSH2)


def _tencent_overview() -> dict[str, Any]:
    from .quotes import tencent_quote

    payload = tencent_quote("sh000001,sz399001,sz399006,sh000688")
    rows = [
        {
            "code": row.get("code"),
            "name": row.get("name"),
            "market": str(row.get("market") or "").lower(),
            "price": row.get("price"),
            "high": row.get("high"),
            "low": row.get("low"),
            "open": row.get("open"),
            "volume": row.get("volume"),
            "amount": row.get("amount"),
            "change": row.get("change"),
            "change_pct": row.get("change_pct"),
        }
        for row in payload.get("data") or []
    ]
    if not rows:
        return empty("tencent", "index snapshot unavailable")
    return frame(
        rows,
        "tencent",
        fallback_from="eastmoney",
        note="push2 不可达，已降级为腾讯指数快照；涨跌家数需 push2 或交易所统计",
    )


@tool(layer="market", ttl="realtime")
def limit_up_pool(date: str = "") -> dict[str, Any]:
    """Limit-up pool (涨停板) for a trading date, including streak and seal time."""
    return _topic_pool("limit_up", date)


@tool(layer="market", ttl="realtime")
def limit_down_pool(date: str = "") -> dict[str, Any]:
    """Limit-down pool (跌停板) for a trading date."""
    return _topic_pool("limit_down", date)


@tool(layer="market", ttl="realtime")
def broken_board_pool(date: str = "") -> dict[str, Any]:
    """Broken limit-up pool (炸板) for a trading date."""
    return _topic_pool("broken", date)


@tool(layer="market", ttl="realtime")
def strong_stock_pool(date: str = "") -> dict[str, Any]:
    """Strong-stock pool (强势股池) for a trading date."""
    return _topic_pool("strong", date)


def _topic_pool(kind: str, date: str) -> dict[str, Any]:
    # Each pool rejects a sort field the other pools accept; the API answers an
    # empty pool instead of an error, so the sort has to travel with the kind.
    endpoint, label, sort = _TOPIC_POOLS[kind]
    day = compact_date(date) if date else last_trading_day().replace("-", "")
    try:
        payload = em_get(
            f"{_PUSH2EX}/{endpoint}",
            params={
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "dpt": "wz.ztzt",
                "Pageindex": 0,
                "pagesize": 1000,
                "sort": sort,
                "date": day,
            },
        )
    except TickHubError:
        raise
    data = (payload or {}).get("data") or {}
    pool = data.get("pool") or []
    if not pool:
        return empty("eastmoney", f"{label} empty (non-trading day or pool not published)", date=day)

    rows = []
    for row in pool:
        stat = row.get("zttj") or {}
        rows.append(
            {
                "code": row.get("c"),
                "name": row.get("n"),
                "market": "sh" if to_int(row.get("m")) == 1 else "sz",
                "price": round(to_float(row.get("p")) / 1000, 3) if to_float(row.get("p")) is not None else None,
                "change_pct": round(to_float(row.get("zdp")) or 0.0, 4),
                "amount": to_float(row.get("amount")),
                "float_market_cap": to_float(row.get("ltsz")),
                "total_market_cap": to_float(row.get("tshare")),
                "turnover_rate": to_float(row.get("hs")),
                "streak": to_int(row.get("lbc")),
                "first_seal_time": str(row.get("fbt") or "").zfill(6) if row.get("fbt") is not None else None,
                "last_seal_time": str(row.get("lbt") or "").zfill(6) if row.get("lbt") is not None else None,
                "seal_amount": to_float(row.get("fund")),
                "broken_times": to_int(row.get("zbc")),
                "industry": row.get("hybk"),
                "limit_up_days": to_int(stat.get("days")),
                "limit_up_count": to_int(stat.get("ct")),
            }
        )
    return ok(
        {"date": day, "pool": label, "items": rows},
        "eastmoney",
        extra={"source_url": f"{_PUSH2EX}/{endpoint}", "count": len(rows)},
    )


@tool(layer="market", ttl="realtime")
def market_sentiment(date: str = "") -> dict[str, Any]:
    """Short-term sentiment gauges derived from limit-up / broken / limit-down pools."""
    day = compact_date(date) if date else last_trading_day().replace("-", "")
    pools: dict[str, dict[str, Any]] = {}
    for kind, label in (("limit_up", "涨停"), ("broken", "炸板"), ("limit_down", "跌停")):
        try:
            payload = _topic_pool(kind, date)
        except TickHubError:
            pools[kind] = {"count": None, "rows": []}
            continue
        body = payload.get("data")
        rows = (body.get("items") if isinstance(body, dict) else []) or []
        pools[kind] = {"count": len(rows), "rows": rows}

    zt = pools["limit_up"]["count"]
    zb = pools["broken"]["count"]
    dt = pools["limit_down"]["count"]
    attempts = (zt or 0) + (zb or 0)
    streak = max([r.get("streak") or 0 for r in pools["limit_up"]["rows"]] or [0])
    if zt is None:
        return empty("eastmoney", "sentiment unavailable", date=day)
    return ok(
        {
            "date": day,
            "limit_up": zt,
            "broken": zb,
            "limit_down": dt,
            "seal_rate": round((zt or 0) / attempts, 4) if attempts else None,
            "max_streak": streak,
            "broken_rate": round((zb or 0) / attempts, 4) if attempts else None,
            "leaders": [
                {"code": r.get("code"), "name": r.get("name"), "streak": r.get("streak")}
                for r in sorted(pools["limit_up"]["rows"], key=lambda x: -(x.get("streak") or 0))[:10]
            ],
        },
        "eastmoney",
        extra={"source_url": f"{_PUSH2EX}/getTopicZTPool"},
    )


@tool(layer="market", ttl="realtime")
def stock_changes(type_codes: str = "", page: int = 1, page_size: int = 100) -> dict[str, Any]:
    """Intraday tick anomalies (盘口异动): fast moves, block buys/sells, seal events."""
    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 100)), 500)
    types = ",".join(str(t).strip() for t in str(type_codes or "").split(",") if str(t).strip()) or ",".join(
        str(t) for t in sorted(_CHANGE_TYPES)
    )
    payload = em_get(
        f"{_PUSH2EX}/getAllStockChanges",
        params={
            "type": types,
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "pageindex": page - 1,
            "pagesize": page_size,
            "dpt": "wzchanges",
        },
    )
    data = (payload or {}).get("data") or {}
    rows = []
    for row in data.get("allstock") or []:
        info = str(row.get("i") or "").split(",")
        t = to_int(row.get("t"))
        rows.append(
            {
                "time": str(row.get("tm") or "").zfill(6),
                "code": row.get("c"),
                "name": row.get("n"),
                "market": "sh" if to_int(row.get("m")) == 1 else "sz",
                "type_code": t,
                "type": _CHANGE_TYPES.get(t or -1, f"异动{t}"),
                "info": info,
            }
        )
    if not rows:
        return empty("eastmoney", "no anomaly records for this window")
    return frame(
        rows,
        "eastmoney",
        source_url=f"{_PUSH2EX}/getAllStockChanges",
        total=to_int(data.get("tc"), len(rows)),
        page=page,
        page_size=page_size,
    )


@tool(layer="market", ttl="realtime")
def dragon_tiger(date: str = "", code: str = "", page_size: int = 100) -> dict[str, Any]:
    """Dragon-tiger list (龙虎榜) for a date and/or a single stock."""
    size = min(max(1, int(page_size or 100)), 500)
    filters = []
    if date:
        filters.append(f"(TRADE_DATE='{require_date(date)}')")
    if code:
        filters.append(f'(SECURITY_CODE="{code.strip()}")')
    rows = em_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str="".join(filters),
        page_size=size,
        page_number=1,
        sort_columns="TRADE_DATE,SECURITY_CODE",
        sort_types="-1,1",
    )
    if not rows:
        return empty("eastmoney", "dragon-tiger list empty", date=date or None)
    out = []
    for r in rows:
        buy = to_float(r.get("BILLBOARD_BUY_AMT"))
        sell = to_float(r.get("BILLBOARD_SELL_AMT"))
        out.append(
            {
                "date": str(r.get("TRADE_DATE") or "")[:10],
                "code": r.get("SECURITY_CODE"),
                "name": r.get("SECURITY_NAME_ABBR"),
                "close": to_float(r.get("CLOSE_PRICE")),
                "change_pct": to_float(r.get("CHANGE_RATE")),
                "turnover_rate": to_float(r.get("TURNOVERRATE")),
                "amount": to_float(r.get("ACCUM_AMOUNT")),
                "buy_amount": buy,
                "sell_amount": sell,
                "net_amount": to_float(r.get("BILLBOARD_NET_AMT")),
                "deal_amount": to_float(r.get("BILLBOARD_DEAL_AMT")),
                "net_ratio": to_float(r.get("DEAL_NET_RATIO")),
                "reason": r.get("EXPLANATION") or r.get("EXPLAIN"),
            }
        )
    return frame(out, "eastmoney")


@tool(layer="market", ttl="realtime")
def st_stocks(page: int = 1, page_size: int = 200) -> dict[str, Any]:
    """Risk-warning (ST / *ST) board list; falls back to baostock when Eastmoney is down."""
    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 200)), 1000)
    try:
        payload = _clist(
            "m:0+t:6+f:4,m:0+t:80+f:4,m:1+t:2+f:4,m:1+t:23+f:4",
            "f12,f14,f2,f3,f4,f5,f6,f8,f9,f10,f20,f21,f23,f62",
            page=page,
            page_size=page_size,
        )
        rows = [
            {
                "code": r.get("f12"),
                "name": r.get("f14"),
                "price": to_float(r.get("f2")),
                "change_pct": to_float(r.get("f3")),
                "volume": to_int(r.get("f5")),
                "amount": to_float(r.get("f6")),
                "turnover_rate": to_float(r.get("f8")),
                "pe": to_float(r.get("f9")),
                "pb": to_float(r.get("f23")),
                "float_market_cap": to_float(r.get("f20")),
                "total_market_cap": to_float(r.get("f21")),
            }
            for r in payload["items"]
        ]
        if rows:
            return frame(rows, "eastmoney", total=payload["total"], page=page, page_size=page_size)
    except TickHubError:
        pass
    from .financials import baostock_st_list

    fallback = baostock_st_list()
    fallback.setdefault("meta", {})["fallback_from"] = "eastmoney"
    return fallback


@tool(layer="market", ttl="realtime")
def ths_hot_rank(list_type: str = "hour") -> dict[str, Any]:
    """同花顺 hot list: heat ranking with the vendor's short commentary."""
    kind = str(list_type or "hour").strip().lower()
    if kind not in {"hour", "day"}:
        raise TickHubError("QFSD-9001", "list_type must be 'hour' or 'day'")
    # The endpoint answers POST with {"status_code": -1, "status_msg": "Param Error."};
    # the identical parameters only work when sent as a query string.
    payload = request(
        "ths",
        "GET",
        _THS_HOT,
        params={"stock_type": "a", "type": kind, "list_type": "normal"},
        encoding="utf-8",
    )
    data = (payload or {}).get("data") or {}
    rows = []
    for i, r in enumerate(data.get("stock_list") or [], 1):
        rows.append(
            {
                "rank": i,
                "code": str(r.get("code") or ""),
                "name": r.get("name"),
                "market": r.get("market"),
                "heat": to_float(r.get("rate")),
                "change_pct": to_float(r.get("rise_and_fall")),
                "tag": r.get("tag"),
                "analysis": r.get("analyse"),
            }
        )
    if not rows:
        status = (payload or {}).get("status_code")
        if status not in (0, None):
            message = (payload or {}).get("status_msg") or status
            return empty("ths", f"同花顺 hot list 拒绝请求：{message}", status_code=status)
        return empty("ths", "hot list empty")
    return frame(rows, "ths", source_url=_THS_HOT, list_type=kind)


@tool(layer="market", ttl="realtime")
def eastmoney_hot_rank(page_size: int = 100) -> dict[str, Any]:
    """Fallback heat ranking from Eastmoney's 股吧 popularity board."""
    size = min(max(1, int(page_size or 100)), 200)
    payload = request(
        "eastmoney",
        "POST",
        _EM_RANK,
        data=json.dumps(
            {
                "appId": "appId01",
                "globalId": "786e4c21-70dc-435a-93bb-38",
                "marketType": "",
                "pageNo": 1,
                "pageSize": size,
            }
        ),
        headers={"Content-Type": "application/json"},
    )
    rows = []
    if isinstance(payload, list):
        items = payload
    else:
        items = (payload or {}).get("data") or []
        if isinstance(items, dict):
            items = items.get("list") or []
    for i, r in enumerate(items, 1):
        raw_code = str(r.get("sc") or r.get("code") or "").strip()
        try:
            code = normalize_code(raw_code) if raw_code else None
        except ValueError:
            code = None
        rows.append(
            {
                "rank": i,
                "code": code,
                "name": r.get("sn") or r.get("name"),
                "heat": to_float(r.get("rk")),
                "change_pct": to_float(r.get("zd")),
                "history_rank": to_int(r.get("hisRc")),
            }
        )
    if not rows:
        return empty("eastmoney", "eastmoney popularity list unavailable")
    return frame(rows, "eastmoney", source_url=_EM_RANK)


def _pool_market(code: Any, m: Any) -> str:
    try:
        return market_of(str(code or ""))
    except ValueError:
        return "sh" if to_int(m) == 1 else "sz"


def _fmt_hhmmss(value: Any) -> str | None:
    """Pool timestamps arrive as ``92500``-style integers."""
    stamp = to_int(value)
    if stamp is None:
        return None
    s = str(stamp).zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


@tool(layer="market", ttl="realtime")
def yesterday_limit_up_pool(date: str = "") -> dict[str, Any]:
    """Yesterday's limit-up pool with today's follow-through (晋级率 input)."""
    day = compact_date(date) if date else last_trading_day().replace("-", "")
    payload = em_get(
        f"{_PUSH2EX}/getYesterdayZTPool",
        params={
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "dpt": "wz.ztzt",
            "Pageindex": 0,
            "pagesize": 1000,
            "sort": "zs:desc",
            "date": day,
        },
    )
    pool = ((payload or {}).get("data") or {}).get("pool") or []
    if not pool:
        return empty("eastmoney", "昨日涨停池 empty (non-trading day or pool not published)", date=day)
    rows = []
    for row in pool:
        stat = row.get("zttj") or {}
        price = to_float(row.get("p"))
        rows.append(
            {
                "code": row.get("c"),
                "name": row.get("n"),
                "market": _pool_market(row.get("c"), row.get("m")),
                "price": round(price / 1000, 3) if price is not None else None,
                "change_pct": to_float(row.get("zdp")),
                "turnover_rate": to_float(row.get("hs")),
                "amplitude": to_float(row.get("zf")),
                "speed": to_float(row.get("zs")),
                "amount": to_float(row.get("amount")),
                "float_market_cap": to_float(row.get("ltsz")),
                "total_market_cap": to_float(row.get("tshare")),
                "y_first_seal_time": _fmt_hhmmss(row.get("yfbt")),
                "y_streak": to_int(row.get("ylbc")),
                "industry": row.get("hybk"),
                "limit_up_days": to_int(stat.get("days")),
                "limit_up_count": to_int(stat.get("ct")),
            }
        )
    return frame(rows, "eastmoney", source_url=f"{_PUSH2EX}/getYesterdayZTPool", date=day)


@tool(layer="market", ttl="realtime")
def monitor_pool(only_active: bool = True) -> dict[str, Any]:
    """Eastmoney key-monitoring list (重点监控) with its validity window.

    ``only_active=True`` keeps only stocks inside the window as of Beijing time.
    """
    payload = request(
        "eastmoney",
        "GET",
        _MONITOR,
        headers={"Referer": "https://vipmoney.eastmoney.com/"},
    )
    rows = payload if isinstance(payload, list) else []
    today = now_shanghai().date().isoformat()
    out = []
    for row in rows:
        start = str(row.get("VALIDATESTARTDATE") or "")
        end = str(row.get("VALIDATEENDDATE") or "")
        if only_active and not (start <= today <= end):
            continue
        raw_market = str(row.get("MARKET") or "").upper()
        out.append(
            {
                "code": row.get("STKCODE"),
                "name": row.get("STKNAME"),
                # Unknown values are passed through (?X) instead of guessing a market.
                "market": _MONITOR_MARKET.get(raw_market, f"?{raw_market}"),
                "start": start,
                "end": end,
            }
        )
    if not out:
        note = "no stock is under monitoring today" if only_active else "monitor list empty"
        return empty("eastmoney", note, date=today)
    return frame(out, "eastmoney", source_url=_MONITOR, date=today, only_active=bool(only_active))


def _anomaly_get(path: str, page_size: int, page_no: int, **extra: Any) -> dict[str, Any]:
    size = min(max(1, int(page_size or 200)), 500)
    page = max(1, int(page_no or 1))
    payload = em_get(
        f"{_ANOMALY}/{path}",
        params={**_HQ_PARAMS, "pageSize": str(size), "pageNo": str(page), **extra},
        headers={"Referer": "https://vipmoney.eastmoney.com/"},
    )
    data = payload or {}
    if to_int(data.get("result")) != 0:
        # result!=0 is a rejection ("unknow team" etc.), not an empty result set.
        raise TickHubError(
            "QFSD-9003",
            f"东财异动接口拒绝: result={data.get('result')} msg={data.get('msg')!r}",
            detail={"path": path},
        )
    return data


def _anomaly_market(code: Any, m: Any, board: Any) -> str:
    """BSE and Shenzhen both come back as m=0, so the code range decides first."""
    c = str(code or "")
    if c.startswith(("4", "8", "92")) or to_int(board) == 8:
        return "BJ"
    return "SH" if to_int(m) == 1 else "SZ"


@tool(layer="market", ttl="realtime")
def price_anomaly(page_size: int = 200, page_no: int = 1) -> dict[str, Any]:
    """Abnormal-move detail (严重异常波动) for the current session."""
    payload = _anomaly_get("list", page_size, page_no)
    rows = []
    for row in payload.get("data") or []:
        rule_code = to_int(row.get("e"))
        if to_int(row.get("s")) == 6 and rule_code in (4, 5, 6, 7):
            rule_code = rule_code * 10
        rows.append(
            {
                "code": row.get("c"),
                "name": row.get("n"),
                "market": _anomaly_market(row.get("c"), row.get("m"), row.get("s")),
                "change_pct": to_float(row.get("a")),
                "deviation": to_float(row.get("x")),
                "days": to_int(row.get("d")),
                "board": to_int(row.get("s")),
                "rule_code": rule_code,
                "rule": _ANOMALY_RULES.get(rule_code, f"未知规则码 {rule_code}"),
                # o==2 means the record is a historical entry, not today's.
                "is_today": to_int(row.get("o")) != 2,
            }
        )
    if not rows:
        return empty("eastmoney", "no abnormal-move record for this session", date=payload.get("date"))
    return frame(
        rows,
        "eastmoney",
        source_url=f"{_ANOMALY}/list",
        date=payload.get("date"),
        pages=to_int(payload.get("pages")),
    )


@tool(layer="market", ttl="realtime")
def price_anomaly_count(
    page_size: int = 50, page_no: int = 1, sort_key: str = "", sort_dir: str = ""
) -> dict[str, Any]:
    """Abnormal-move tally per stock (异动次数 + 现价)."""
    payload = _anomaly_get("count", page_size, page_no, sortKey=sort_key, sortDir=sort_dir)
    rows = []
    for row in payload.get("data") or []:
        rows.append(
            {
                "code": row.get("c"),
                "name": row.get("n"),
                "market": _anomaly_market(row.get("c"), row.get("m"), row.get("s")),
                "price": to_float(row.get("p")),
                "change_pct": to_float(row.get("a")),
                # Unlike /list, t here is the number of abnormal moves.
                "times": to_int(row.get("t")),
                "deviation": to_float(row.get("x")),
                "days": to_int(row.get("d")),
                "board": to_int(row.get("s")),
            }
        )
    if not rows:
        return empty("eastmoney", "no abnormal-move tally for this session", date=payload.get("date"))
    return frame(
        rows,
        "eastmoney",
        source_url=f"{_ANOMALY}/count",
        date=payload.get("date"),
        pages=to_int(payload.get("pages")),
    )


def _unix_hhmmss(value: Any) -> str:
    stamp = to_int(value)
    if stamp is None:
        return ""
    moment = _dt.datetime.fromtimestamp(stamp, tz=_dt.timezone(_dt.timedelta(hours=8)))
    return moment.strftime("%H:%M:%S")


@tool(layer="market", ttl="realtime")
def ths_limit_up_pool(date: str = "") -> dict[str, Any]:
    """THS limit-up reveal (涨停揭秘): limit reason, board type and seal quality."""
    day = compact_date(date) if date else last_trading_day().replace("-", "")
    payload = request(
        "ths",
        "GET",
        _THS_ZT,
        params={
            "page": 1,
            "limit": 200,
            "field": _THS_ZT_FIELDS,
            "filter": "HS,GEM2STAR",
            "order_field": "330324",
            "order_type": "0",
            "date": day,
        },
        encoding="utf-8",
    )
    data = (payload or {}).get("data") or {}
    info = data.get("info") or []
    if not info:
        status = to_int((payload or {}).get("status_code"))
        if status not in (0, None):
            message = (payload or {}).get("status_msg") or status
            return empty("ths", f"同花顺涨停揭秘拒绝请求：{message}", date=day, status_code=status)
        return empty("ths", "limit-up reveal empty (non-trading day or not published)", date=day)
    rows = []
    for item in info:
        rows.append(
            {
                "code": item.get("code"),
                "name": item.get("name"),
                "price": to_float(item.get("latest")),
                "change_pct": to_float(item.get("change_rate")),
                "reason": item.get("reason_type") or "",
                "board_type": item.get("limit_up_type") or "",
                "seal_rate": to_float(item.get("limit_up_suc_rate")),
                "break_times": to_int(item.get("open_num"), 0),
                "seal_amount": to_float(item.get("order_amount")),
                "high_days": item.get("high_days") or "",
                "first_time": _unix_hhmmss(item.get("first_limit_up_time")),
                "is_again": to_int(item.get("is_again_limit")),
            }
        )
    return frame(
        rows,
        "ths",
        source_url=_THS_ZT,
        date=day,
        limit_up_count=to_int(data.get("limit_up_count")),
        limit_down_count=to_int(data.get("limit_down_count")),
    )


@tool(layer="market", ttl="realtime")
def stock_concept_blocks(code: str) -> dict[str, Any]:
    """Sector / theme membership of one stock (industry, concept and region mixed)."""
    c = stock_code(code)
    payload = em_get(
        _SLIST,
        params={
            "fltt": "2",
            "invt": "2",
            "secid": eastmoney_secid(c),
            "spt": "3",
            "pi": "0",
            "pz": "200",
            "po": "1",
            "fields": "f12,f14,f3,f128",
        },
        headers={"Referer": "https://quote.eastmoney.com/"},
    )
    diff = ((payload or {}).get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    boards = [
        {
            "name": row.get("f14"),
            "code": row.get("f12"),
            "change_pct": to_float(row.get("f3")),
            "lead_stock": row.get("f128"),
        }
        for row in diff
    ]
    if not boards:
        return empty("eastmoney", f"{c} 没有返回板块归属", code=c)
    return ok(
        {"code": c, "total": len(boards), "boards": boards, "concept_tags": [b["name"] for b in boards]},
        "eastmoney",
        extra={"source_url": _SLIST, "count": len(boards)},
    )


def _seat_rows(report: str, latest: str, code: str, sort_column: str) -> list[dict[str, Any]]:
    return _strict_rows(
        report,
        f"(TRADE_DATE='{latest}')(SECURITY_CODE=\"{code}\")",
        sort_column,
        "-1",
        10,
        narrowed=True,
        equal={"SECURITY_CODE": code},
        dates={"TRADE_DATE": (latest, latest)},
    )


@tool(layer="market", ttl="semi_static")
def dragon_tiger_seats(code: str, trade_date: str = "", look_back: int = 30) -> dict[str, Any]:
    """Dragon-tiger seats (席位) for one stock: records, buy/sell TOP5 and institution flow.

    Amounts are in 万元. ``look_back`` counts calendar days before ``trade_date``.
    """
    c = stock_code(code)
    end = require_date(trade_date or None, last_trading_day())
    window = min(max(1, int(look_back or 30)), 365)
    start = (_dt.date.fromisoformat(end) - _dt.timedelta(days=window)).isoformat()
    raw = _strict_rows(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')(SECURITY_CODE=\"{c}\")",
        "TRADE_DATE",
        "-1",
        50,
        narrowed=True,
        equal={"SECURITY_CODE": c},
        dates={"TRADE_DATE": (start, end)},
    )
    records = [
        {
            "date": str(row.get("TRADE_DATE") or "")[:10],
            "reason": row.get("EXPLANATION") or "",
            "net_buy": round((to_float(row.get("BILLBOARD_NET_AMT")) or 0.0) / 10000, 1),
            "turnover": round(to_float(row.get("TURNOVERRATE")) or 0.0, 2),
        }
        for row in raw
    ]
    buy_rows: list[dict[str, Any]] = []
    sell_rows: list[dict[str, Any]] = []
    seats: dict[str, list[dict[str, Any]]] = {"buy": [], "sell": []}
    latest = ""
    if records:
        latest = records[0]["date"]
        buy_rows = _seat_rows("RPT_BILLBOARD_DAILYDETAILSBUY", latest, c, "BUY")
        sell_rows = _seat_rows("RPT_BILLBOARD_DAILYDETAILSSELL", latest, c, "SELL")
        for side, rows in (("buy", buy_rows[:5]), ("sell", sell_rows[:5])):
            for row in rows:
                seats[side].append(
                    {
                        "name": row.get("OPERATEDEPT_NAME") or "",
                        "buy_amt": round((to_float(row.get("BUY")) or 0.0) / 10000, 1),
                        "sell_amt": round((to_float(row.get("SELL")) or 0.0) / 10000, 1),
                        "net": round((to_float(row.get("NET")) or 0.0) / 10000, 1),
                    }
                )
    # OPERATEDEPT_CODE "0" is the institution-only seat.
    inst_buy = sum((to_float(row.get("BUY")) or 0.0) for row in buy_rows if str(row.get("OPERATEDEPT_CODE", "")) == "0")
    inst_sell = sum(
        (to_float(row.get("SELL")) or 0.0) for row in sell_rows if str(row.get("OPERATEDEPT_CODE", "")) == "0"
    )
    institution = {
        "buy_amt": round(inst_buy / 10000, 1),
        "sell_amt": round(inst_sell / 10000, 1),
        "net_amt": round((inst_buy - inst_sell) / 10000, 1),
    }
    return ok(
        {"code": c, "records": records, "seats": seats, "institution": institution, "latest_date": latest or None},
        "eastmoney",
        extra={"source_url": DATACENTER, "count": len(records)},
    )
