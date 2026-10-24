"""Index data: quotes, constituents, valuation, calendar and SW industries."""

from __future__ import annotations

import datetime as _dt
from typing import Any

from ..core.errors import TickHubError
from ..core.http import em_get
from ..core.ticker import is_index, normalize_code, parse_date
from .base import tool
from .emdc import strict_rows
from .util import empty, ok, to_float, to_int

_PUSH2 = "https://push2.eastmoney.com/api/qt/stock/get"
_PUSH2HIS = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

# Eastmoney datacenter component-report ids. The report keys members by an
# internal TYPE number rather than the index code, so only indices confirmed by
# constituent count, market mix and published weights are mapped; an unmapped
# code answers with an explicit note instead of another index's members.
_INDEX_TYPE = {
    "000300": "1",  # 沪深300
    "000016": "2",  # 上证50
    "000905": "3",  # 中证500
    "000688": "4",  # 科创50
    "930050": "5",  # 中证A50
    "000510": "6",  # 中证A500
    "000852": "7",  # 中证1000
    "399850": "8",  # 深证50
    "399005": "9",  # 中小100
    "899050": "10",  # 北证50
    "000010": "11",  # 上证180
    "000903": "12",  # 中证100
    "932000": "13",  # 中证2000
}


def _secid(code: str) -> str:
    c = normalize_code(code)
    return f"{'1' if c.startswith(('000', '5', '6', '9')) else '0'}.{c}"


@tool(layer="index", ttl="realtime")
def index_kline(code: str = "000300", days: int = 120) -> dict[str, Any]:
    """Daily K-line for a mainland index (SSE/SZSE/CSI/STAR)."""
    c = normalize_code(code)
    if not is_index(code):
        raise TickHubError("QFSD-9001", f"not an index code: {code}")
    limit = min(max(1, int(days or 120)), 2000)
    try:
        payload = em_get(
            _PUSH2HIS,
            params={
                "secid": _secid(c),
                "klt": 101,
                "fqt": 0,
                "lmt": limit,
                "end": "20500101",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            },
        )
    except TickHubError:
        return _tencent_index_kline(c, limit)
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
                "change_pct": to_float(p[8]) if len(p) > 8 else None,
            }
        )
    if not rows:
        return _tencent_index_kline(c, limit)
    return ok({"code": c, "name": data.get("name"), "klines": rows}, "eastmoney", extra={"count": len(rows)})


def _index_symbol(code: str) -> str:
    """Tencent index symbol: 000xxx (CSI) lives on SH, 399xxx on SZ."""
    c = normalize_code(code)
    return ("sz" if c.startswith(("399", "980")) else "sh") + c


def _tencent_index_kline(code: str, limit: int) -> dict[str, Any]:
    from .quotes import tencent_kline

    payload = tencent_kline(_index_symbol(code), period="daily", adjust="none", limit=limit)
    rows = payload["data"]["klines"]
    previous = None
    for row in rows:
        if previous and previous > 0 and row.get("close") is not None:
            row["change_pct"] = round((row["close"] - previous) / previous * 100, 4)
        previous = row.get("close") or previous
    if not rows:
        return empty("tencent", "index kline unavailable", code=code)
    return ok(
        {"code": code, "klines": rows},
        "tencent",
        extra={"count": len(rows), "fallback_from": "eastmoney"},
    )


def _tencent_index_valuation(code: str) -> dict[str, Any]:
    from .quotes import tencent_quote

    payload = tencent_quote(_index_symbol(code))
    rows = payload.get("data") or []
    if not rows:
        return empty("tencent", "index valuation unavailable", code=code)
    row = rows[0]
    return ok(
        {
            "code": row.get("code") or code,
            "name": row.get("name"),
            "price": row.get("price"),
            "change": row.get("change"),
            "change_pct": row.get("change_pct"),
            "high": row.get("high"),
            "low": row.get("low"),
            "open": row.get("open"),
            "volume": row.get("volume"),
            "amount": row.get("amount"),
            "turnover_rate": None,
            "pe": None,
            "pb": None,
            "note": "push2 不可达，已降级为腾讯实时快照；指数 PE/PB 需交易所或指数公司文件",
        },
        "tencent",
        extra={"fallback_from": "eastmoney"},
    )


@tool(layer="index", ttl="realtime")
def index_valuation(code: str = "000300") -> dict[str, Any]:
    """Index level, turnover and (when published) PE/PB multiples."""
    c = normalize_code(code)
    if not is_index(code):
        raise TickHubError("QFSD-9001", f"not an index code: {code}")
    try:
        payload = em_get(
            _PUSH2,
            params={"secid": _secid(c), "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f162,f167,f168,f169,f170"},
        )
    except TickHubError:
        return _tencent_index_valuation(c)
    data = (payload or {}).get("data") or {}
    if not data:
        return _tencent_index_valuation(c)
    pe = to_float(data.get("f162"))
    pb = to_float(data.get("f167"))
    return ok(
        {
            "code": data.get("f57") or c,
            "name": data.get("f58"),
            "price": to_float(data.get("f43")),
            "change": to_float(data.get("f169")),
            "change_pct": to_float(data.get("f170")),
            "high": to_float(data.get("f44")),
            "low": to_float(data.get("f45")),
            "open": to_float(data.get("f46")),
            "volume": to_int(data.get("f47")),
            "amount": to_float(data.get("f48")),
            "turnover_rate": to_float(data.get("f168")),
            "pe": pe or None,
            "pb": pb or None,
            "note": None if pe else "该指数未发布市盈率/市净率快照",
        },
        "eastmoney",
    )


def _component_rows(type_id: str) -> list[dict[str, Any]]:
    return strict_rows(
        "RPT_INDEX_TS_COMPONENT",
        f'(TYPE="{type_id}")',
        "SECURITY_CODE",
        "1",
        5000,
        narrowed=True,
        equal={"TYPE": type_id},
    )


def _component_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": row.get("SECURITY_CODE"),
        "name": row.get("SECURITY_NAME_ABBR"),
        "exchange": str(row.get("SECUCODE") or "").rsplit(".", 1)[-1] or None,
        "industry": row.get("INDUSTRY"),
        "price": to_float(row.get("CLOSE_PRICE")),
        "change_pct": to_float(row.get("CHANGE_RATE")),
        "weight": to_float(row.get("WEIGHT")),
        "pe": to_float(row.get("PE")),
        "eps": to_float(row.get("EPS")),
        "bps": to_float(row.get("BPS")),
        "total_shares": to_float(row.get("TOTAL_SHARES")),
        "free_shares": to_float(row.get("FREE_SHARES")),
        "free_cap": to_float(row.get("FREE_CAP")),
        "as_of": str(row.get("MAXTRADEDATE") or "")[:10] or None,
    }


@tool(layer="index", ttl="semi_static")
def index_constituents(index_code: str = "000300") -> dict[str, Any]:
    """Constituent list for a supported mainland index (Eastmoney datacenter)."""
    c = normalize_code(index_code)
    type_id = _INDEX_TYPE.get(c)
    if type_id is None:
        if not is_index(index_code):
            raise TickHubError("QFSD-9001", f"not an index code: {index_code}")
        return empty(
            "eastmoney",
            f"该指数尚未纳入成分表，可用代码：{', '.join(_INDEX_TYPE)}",
            index_code=c,
        )
    rows = [_component_row(row) for row in _component_rows(type_id)]
    if not rows:
        return empty("eastmoney", "东财成分表返回 0 行", index_code=c, index_type=type_id)
    as_of = max((row["as_of"] for row in rows if row["as_of"]), default=None)
    return ok(
        {"index_code": c, "constituents": rows},
        "eastmoney",
        data_as_of=as_of,
        extra={"count": len(rows), "index_type": type_id},
    )


@tool(layer="index", ttl="semi_static")
def index_weights(index_code: str = "000300") -> dict[str, Any]:
    """Index constituent weights from the Eastmoney component report."""
    c = normalize_code(index_code)
    payload = index_constituents(index_code)
    body = payload.get("data")
    rows = (body.get("constituents") if isinstance(body, dict) else []) or []
    if not rows:
        note = (payload.get("meta") or {}).get("note") or "指数成分不可用"
        return empty("eastmoney", note, index_code=c)
    weighted = [{"code": row["code"], "name": row["name"], "weight": row.get("weight")} for row in rows]
    covered = [w for w in weighted if w["weight"] is not None]
    if not covered:
        return empty("eastmoney", "东财未发布该指数权重（沪深300/上证50/中证500/科创50 有权重）", index_code=c)
    as_of = max((row["as_of"] for row in rows if row["as_of"]), default=None)
    return ok(
        {"index_code": c, "weights": weighted},
        "eastmoney",
        data_as_of=as_of,
        extra={
            "count": len(weighted),
            "covered": len(covered),
            "total_weight": round(sum(w["weight"] for w in covered), 2),
        },
    )


@tool(layer="index", ttl="static")
def trading_calendar(start: str = "", end: str = "", limit: int = 800) -> dict[str, Any]:
    """Trading dates derived from the SSE composite daily K-line."""
    limit = min(max(2, int(limit or 800)), 5000)
    try:
        payload = em_get(
            _PUSH2HIS,
            params={
                "secid": "1.000001",
                "klt": 101,
                "fqt": 0,
                "lmt": limit,
                "end": "20500101",
                "fields1": "f1",
                "fields2": "f51",
            },
        )
    except TickHubError:
        return _tencent_trading_calendar(start, end, limit)
    data = (payload or {}).get("data") or {}
    days = [line.split(",")[0] for line in data.get("klines") or [] if line]
    if not days:
        return _tencent_trading_calendar(start, end, limit)
    lo = parse_date(start) if start else days[0]
    hi = parse_date(end) if end else days[-1]
    selected = [d for d in days if lo <= d <= hi]
    return ok(
        {"start": lo, "end": hi, "days": selected, "count": len(selected)},
        "exchange",
        extra={"count": len(selected)},
    )


def _tencent_trading_calendar(start: str, end: str, limit: int) -> dict[str, Any]:
    fallback = _tencent_index_kline("sh000001", limit)
    days = [row["date"] for row in fallback["data"]["klines"]]
    if not days:
        return empty("exchange", "trading calendar unavailable")
    lo = parse_date(start) if start else days[0]
    hi = parse_date(end) if end else days[-1]
    selected = [d for d in days if lo <= d <= hi]
    return ok(
        {"start": lo, "end": hi, "days": selected, "count": len(selected)},
        "tencent",
        extra={"count": len(selected), "fallback_from": "eastmoney"},
    )


@tool(layer="index", ttl="static")
def is_trading_day(date: str = "") -> dict[str, Any]:
    """Whether a date is an exchange trading day."""
    day = parse_date(date)
    payload = trading_calendar(start=day, end=day, limit=30)
    days = (payload.get("data") or {}).get("days") or []
    return ok({"date": day, "is_trading_day": day in days}, "exchange")


@tool(layer="index", ttl="semi_static")
def index_list(scope: str = "all") -> dict[str, Any]:
    """List mainland indices by market (all / sh / sz / csi / star)."""
    key = str(scope or "all").strip().lower()
    fs_map = {
        "all": "m:1+s:2,m:0+s:2",
        "sh": "m:1+s:2",
        "sz": "m:0+s:2",
        "star": "m:1+s:2+f:!50",
    }
    if key not in fs_map:
        raise TickHubError("QFSD-9001", "scope must be one of all/sh/sz/star")
    payload = em_get(
        "https://push2.eastmoney.com/api/qt/clist/get",
        params={
            "pn": 1,
            "pz": 500,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": fs_map[key],
            "fields": "f12,f14,f2,f3,f4,f5,f6",
        },
    )
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
        }
        for r in raw
    ]
    if not rows:
        return empty("eastmoney", "index list unavailable", scope=key)
    return ok({"scope": key, "indices": rows}, "eastmoney", extra={"count": len(rows)})


@tool(layer="index", ttl="semi_static")
def sw_industry_index(code: str, days: int = 120) -> dict[str, Any]:
    """Shenwan industry index history (best effort across Eastmoney secid layouts)."""
    c = normalize_code(code)
    limit = min(max(1, int(days or 120)), 1000)
    for secid in (f"2.{c}", f"90.{c}"):
        payload = em_get(
            _PUSH2HIS,
            params={
                "secid": secid,
                "klt": 101,
                "fqt": 0,
                "lmt": limit,
                "end": "20500101",
                "fields1": "f1",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            },
        )
        data = (payload or {}).get("data") or {}
        klines = data.get("klines") or []
        if klines:
            rows = []
            for line in klines:
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
                    }
                )
            return ok({"code": c, "name": data.get("name"), "klines": rows}, "eastmoney", extra={"count": len(rows)})
    return empty("eastmoney", "申万指数历史暂不可用（代码或路由未发布）", code=c)


@tool(layer="index", ttl="static")
def market_calendar_summary(days: int = 30) -> dict[str, Any]:
    """Recent trading days plus today's session state."""
    payload = trading_calendar(limit=max(30, int(days or 30)))
    data = payload.get("data") or {}
    listed = data.get("days") or []
    now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
    today = now.date().isoformat()
    return ok(
        {
            "today": today,
            "is_trading_day": today in listed,
            "session": "open" if today in listed and 9 <= now.hour < 15 else "closed",
            "recent_days": listed[-int(days or 30):],
        },
        "exchange",
    )
