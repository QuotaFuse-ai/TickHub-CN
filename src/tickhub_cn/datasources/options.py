"""ETF options: contract chain, T-quote and exchange-computed Greeks (Sina)."""

from __future__ import annotations

import re
from typing import Any

from ..core.errors import TickHubError
from ..core.http import request
from .base import tool
from .util import ok, to_float

_SINA_HQ = "https://hq.sinajs.cn/list={param}"
_SINA_MONTHS = (
    "https://stock.finance.sina.com.cn/futures/api/openapi.php/"
    "StockOptionService.getStockName?exchange=null&cate={cate}"
)
_SINA_HEADERS = {"Referer": "https://stock.finance.sina.com.cn/"}

_UNDERLYINGS = {
    "510050": "50ETF",
    "510300": "300ETF",
    "588000": "科创50ETF",
    "510500": "500ETF",
}

_OPTION_CODE = re.compile(r"^\d{6,8}$")
_HQ_VALUE = re.compile(r'="([^"]*)"')


def _sina_values(param: str) -> list[str]:
    """Read one ``var hq_str_XXX="a,b,c";`` payload as GBK-separated fields."""
    text = request(
        "sina",
        "GET",
        _SINA_HQ.format(param=param),
        headers=_SINA_HEADERS,
        expect_json=False,
        encoding="gbk",
    )
    match = _HQ_VALUE.search(str(text))
    return match.group(1).split(",") if match else []


def _option_code(code: str) -> str:
    value = str(code or "").strip().upper()
    if value.startswith("CON_OP_"):
        value = value[len("CON_OP_") :]
    if not _OPTION_CODE.match(value):
        raise TickHubError("QFSD-9001", f"invalid option contract code: {code!r}")
    return value


@tool("sina_option_codes", layer="option", ttl="realtime", cache_domain="option")
def sina_option_codes(underlying: str = "510050", call: bool = True) -> dict[str, Any]:
    """ETF option contract chain grouped by expiry month (Sina)."""
    code = str(underlying or "").strip()
    cate = _UNDERLYINGS.get(code)
    if not cate:
        raise TickHubError(
            "QFSD-9001",
            f"unsupported underlying {underlying!r}",
            detail={"supported": sorted(_UNDERLYINGS)},
        )
    payload = request("sina", "GET", _SINA_MONTHS.format(cate=cate), headers=_SINA_HEADERS)
    try:
        months_raw = payload["result"]["data"]["contractMonth"]
    except (KeyError, TypeError) as exc:
        raise TickHubError("QFSD-9003", "新浪期权月份接口返回结构已变", detail=str(payload)[:200]) from exc
    # The first entry is the spot month that has already expired; the site drops it too.
    months = [str(m).replace("-", "")[2:] for m in (months_raw or [])[1:]]
    flag = "OP_UP_" if call else "OP_DOWN_"
    chains: dict[str, list[str]] = {}
    for month in months:
        contracts = [
            item[len("CON_OP_") :]
            for item in _sina_values(f"{flag}{code}{month}")
            if item.startswith("CON_OP_")
        ]
        if contracts:
            chains[month] = contracts
    if not chains:
        raise TickHubError(
            "QFSD-9003",
            f"{cate} {'认购' if call else '认沽'}合约清单为空，接口可能已变更",
            detail={"months": months},
        )
    flat = [contract for contracts in chains.values() for contract in contracts]
    return ok(
        {
            "underlying": code,
            "cate": cate,
            "call": bool(call),
            "months": chains,
            "near_month": next(iter(chains)),
            "contracts": flat,
        },
        "sina",
        extra={"count": len(flat)},
    )


@tool("sina_option_tquote", layer="option", ttl="realtime", cache_domain="option")
def sina_option_tquote(code: str) -> dict[str, Any]:
    """Option T-quote: bid/ask five levels, open interest, strike and limits (Sina)."""
    contract = _option_code(code)
    values = _sina_values(f"CON_OP_{contract}")
    if len(values) < 43:
        raise TickHubError(
            "QFSD-9005",
            f"期权合约 {contract} 无报价（可能已到期或代码不存在）",
            detail={"fields": len(values)},
        )
    return ok(
        {
            "code": contract,
            "bid_vol": to_float(values[0]),
            "bid": to_float(values[1]),
            "last": to_float(values[2]),
            "ask": to_float(values[3]),
            "ask_vol": to_float(values[4]),
            "open_interest": to_float(values[5]),
            "pct": to_float(values[6]),
            "strike": to_float(values[7]),
            "prev_close": to_float(values[8]),
            "open": to_float(values[9]),
            "limit_up": to_float(values[10]),
            "limit_down": to_float(values[11]),
            "name": values[37],
            "amplitude": to_float(values[38]),
            "high": to_float(values[39]),
            "low": to_float(values[40]),
            "volume": to_float(values[41]),
            "amount": to_float(values[42]),
        },
        "sina",
    )


@tool("sina_option_greeks", layer="option", ttl="realtime", cache_domain="option")
def sina_option_greeks(code: str) -> dict[str, Any]:
    """Exchange-computed Greeks, implied volatility and theoretical value (Sina)."""
    contract = _option_code(code)
    raw = _sina_values(f"CON_SO_{contract}")
    if len(raw) < 16:
        raise TickHubError(
            "QFSD-9005",
            f"期权合约 {contract} 无希腊字母数据（中金所仅对部分合约提供）",
            detail={"fields": len(raw)},
        )
    # raw[1:4] are three empty placeholders; keeping them shifts every field.
    values = [raw[0]] + raw[4:]
    return ok(
        {
            "code": contract,
            "name": values[0],
            "volume": to_float(values[1]),
            "delta": to_float(values[2]),
            "gamma": to_float(values[3]),
            "theta": to_float(values[4]),
            "vega": to_float(values[5]),
            "iv": to_float(values[6]),
            "high": to_float(values[7]),
            "low": to_float(values[8]),
            "trade_code": values[9],
            "strike": to_float(values[10]),
            "last": to_float(values[11]),
            "theory": to_float(values[12]),
        },
        "sina",
    )


__all__ = ["sina_option_codes", "sina_option_greeks", "sina_option_tquote"]
