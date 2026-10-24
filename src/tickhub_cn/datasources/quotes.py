"""Quotes, K-line and tick data (Tencent primary, Sina/Baidu/TDX secondary)."""

from __future__ import annotations

import datetime as _dt
import io
import re
import struct
import zipfile
from pathlib import Path
from typing import Any

from ..core.errors import TickHubError
from ..core.http import request
from ..core.ticker import (
    compact_date,
    ensure_code_list,
    is_index,
    normalize_code,
    tencent_symbol,
)
from .base import run_batch, tool
from .util import code_or_raise, empty, frame, js_json, ok, require_date, to_float, to_int

_QT = "https://qt.gtimg.cn/q="
_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_MIN_KLINE = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
_TICKS = "https://stock.gtimg.cn/data/index.php"
_SINA_QFQ = "https://finance.sina.com.cn/realstock/company/{sym}/qfq.js"
_Baidu = "https://finance.pae.baidu.com/vapi/v1/getquotation"
_TDX = "https://www.tdx.com.cn/products/data/data/vipdoc/{market}lday.zip"


def _parse_qt_line(line: str) -> dict[str, Any] | None:
    if "=" not in line:
        return None
    var, payload = line.split("=", 1)
    payload = payload.strip().strip(";").strip('"')
    parts = payload.split("~")
    if len(parts) < 46:
        return None
    code = parts[2] or ""
    price = to_float(parts[3])
    prev = to_float(parts[4])
    # Tencent uses 1/51/62 for SH/SZ/BJ; the variable prefix is authoritative.
    prefix = var.strip().lower().removeprefix("v_")[:2]
    market = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(prefix)
    if market is None:
        market = {"1": "SH", "51": "SZ", "62": "BJ"}.get(parts[0], "SH")
    return {
        "code": code,
        "name": parts[1],
        "price": price,
        "prev_close": prev,
        "open": to_float(parts[5]),
        "volume": to_int(parts[6]),
        "outside_volume": to_int(parts[7]),
        "inside_volume": to_int(parts[8]),
        "bid1": to_float(parts[9]),
        "bid1_volume": to_int(parts[10]),
        "ask1": to_float(parts[19]),
        "ask1_volume": to_int(parts[20]),
        "timestamp": parts[30],
        "change": to_float(parts[31]),
        "change_pct": to_float(parts[32]),
        "high": to_float(parts[33]),
        "low": to_float(parts[34]),
        "amount": to_float(parts[37]),
        "turnover_rate": to_float(parts[38]),
        "pe": to_float(parts[39]),
        "amplitude": to_float(parts[43]),
        "float_market_cap": to_float(parts[44]),
        "total_market_cap": to_float(parts[45]),
        "pb": to_float(parts[46]) if len(parts) > 46 else None,
        "limit_up": to_float(parts[47]) if len(parts) > 47 else None,
        "limit_down": to_float(parts[48]) if len(parts) > 48 else None,
        "market": market,
        "is_stale": bool(prev and price and parts[6] in ("", "0")) if parts[6] else False,
    }


def _symbol(code: str) -> str:
    """Tencent symbol, keeping an explicit ``sh``/``sz``/``bj`` marker.

    ``000xxx`` is shared by Shanghai indices and Shenzhen stocks, so normalising
    the prefix away would silently quote a different instrument.
    """
    raw = str(code or "").strip()
    if not raw:
        raise TickHubError("QFSD-9001", "code is required")
    try:
        return tencent_symbol(raw)
    except ValueError as exc:
        raise TickHubError("QFSD-9001", str(exc)) from exc


def _quote_rows(codes: str | list[str]) -> list[dict[str, Any]]:
    if isinstance(codes, str):
        parts = [p.strip() for p in re.split(r"[,;\s]+", codes) if p.strip()]
    else:
        parts = [str(c).strip() for c in codes if str(c).strip()]
    if not parts:
        raise TickHubError("QFSD-9001", "at least one code is required")
    symbols = [_symbol(p) for p in parts]
    text = request("tencent", "GET", _QT + ",".join(symbols), expect_json=False, encoding="gbk")
    rows = []
    for line in str(text).splitlines():
        row = _parse_qt_line(line)
        if row:
            rows.append(row)
    return rows


@tool("tencent_quote", layer="quotes", ttl="realtime", cache_domain="quote")
def tencent_quote(codes: str) -> dict[str, Any]:
    """Realtime A-share quote from Tencent (price, PE, PB, turnover, caps)."""
    rows = _quote_rows(codes)
    return envelope_rows(rows, "tencent")


def envelope_rows(rows: list[dict[str, Any]], source: str, **kwargs: Any) -> dict[str, Any]:
    return ok(rows, source, extra={"count": len(rows)}, **kwargs)


@tool("tencent_quote_batch", layer="quotes", ttl="realtime", cache_domain="quote")
def tencent_quote_batch(codes: str, max_codes: int = 50) -> dict[str, Any]:
    """Batch realtime quotes with partial-failure reporting."""
    items = ensure_code_list(codes)
    chunk: list[str] = []
    results: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    for code in items[:max_codes]:
        chunk.append(code)
        if len(chunk) >= 20:
            try:
                for row in _quote_rows(",".join(chunk)):
                    results[row["code"]] = row
            except Exception as exc:  # noqa: BLE001
                failures.extend({"code": c, "error": str(exc)} for c in chunk)
            chunk = []
    if chunk:
        try:
            for row in _quote_rows(",".join(chunk)):
                results[row["code"]] = row
        except Exception as exc:  # noqa: BLE001
            failures.extend({"code": c, "error": str(exc)} for c in chunk)
    payload = ok(
        {"total": len(items[:max_codes]), "succeeded": len(results), "failed": len(failures), "quotes": results, "failures": failures},
        "tencent",
    )
    if failures:
        payload["ok"] = False
        payload["meta"]["error_code"] = "QFSD-8001"
    return payload


@tool("tencent_kline", layer="quotes", ttl="semi_static", cache_domain="kline")
def tencent_kline(
    code: str,
    period: str = "daily",
    adjust: str = "qfq",
    limit: int = 320,
    start: str = "",
    end: str = "",
) -> dict[str, Any]:
    """Daily/weekly/monthly K-line with forward/backward adjustment (Tencent)."""
    sym = _symbol(code)
    period = (period or "daily").lower()
    aliases = {"day": "day", "daily": "day", "1d": "day", "week": "week", "weekly": "week", "month": "month", "monthly": "month"}
    if period not in aliases:
        raise TickHubError("QFSD-9001", f"unsupported period: {period}")
    p = aliases[period]
    adj = (adjust or "qfq").lower()
    if adj not in {"qfq", "hfq", "none", ""}:
        raise TickHubError("QFSD-9001", f"unsupported adjust: {adjust}")
    limit = max(1, min(int(limit or 320), 2000))
    param = f"{sym},{p},{start or ''},{end or ''},{limit},{'' if adj in ('none', '') else adj}"
    data = request("tencent", "GET", _KLINE, params={"param": param})
    node = ((data or {}).get("data") or {}).get(sym) or {}
    key_candidates = [f"{adj}{p}" if adj in {"qfq", "hfq"} else p, p, "qfqday" if p == "day" else f"qfq{p}", "day"]
    raw: list[Any] = []
    for key in key_candidates:
        if isinstance(node.get(key), list) and node[key]:
            raw = node[key]
            break
    rows = []
    for item in raw:
        if not isinstance(item, list) or len(item) < 6:
            continue
        rows.append(
            {
                "date": str(item[0]),
                "open": to_float(item[1]),
                "close": to_float(item[2]),
                "high": to_float(item[3]),
                "low": to_float(item[4]),
                "volume": to_float(item[5]),
                "amount": to_float(item[6]) if len(item) > 6 and not isinstance(item[6], dict) else None,
            }
        )
    if start:
        rows = [r for r in rows if r["date"] >= start]
    if end:
        rows = [r for r in rows if r["date"] <= end]
    return ok({"code": normalize_code(code), "period": period, "adjust": adj, "klines": rows}, "tencent", extra={"count": len(rows)})


@tool("tencent_minute", layer="quotes", ttl="realtime", cache_domain="kline")
def tencent_minute(code: str, period: str = "m5", limit: int = 320) -> dict[str, Any]:
    """Intraday minute K-line (1/5/15/30/60 minutes) from Tencent."""
    sym = _symbol(code)
    period = (period or "m5").lower()
    if period not in {"m1", "m5", "m15", "m30", "m60"}:
        raise TickHubError("QFSD-9001", f"unsupported minute period: {period}")
    limit = max(1, min(int(limit or 320), 1000))
    data = request("tencent", "GET", _MIN_KLINE, params={"param": f"{sym},{period},,{limit}"})
    node = ((data or {}).get("data") or {}).get(sym) or {}
    raw = node.get(period) or []
    rows = []
    for item in raw:
        if not isinstance(item, list) or len(item) < 6:
            continue
        rows.append(
            {
                "time": str(item[0]),
                "open": to_float(item[1]),
                "close": to_float(item[2]),
                "high": to_float(item[3]),
                "low": to_float(item[4]),
                "volume": to_float(item[5]),
            }
        )
    return ok({"code": normalize_code(code), "period": period, "bar": rows}, "tencent", extra={"count": len(rows)})


@tool("tencent_ticks", layer="quotes", ttl="realtime", cache_domain="ticks")
def tencent_ticks(code: str, page: int = 1) -> dict[str, Any]:
    """Tick-by-tick trades for the latest session (Tencent, no BSE)."""
    sym = _symbol(code)
    text = request(
        "tencent",
        "GET",
        _TICKS,
        params={"appn": "detail", "action": "data", "c": sym, "p": str(max(1, int(page or 1)))},
        expect_json=False,
    )
    body = str(text)
    if "=" in body:
        body = body.split("=", 1)[1].strip().rstrip(";")
    seq = js_json(body)
    entries = seq[1] if isinstance(seq, list) and len(seq) > 1 else ""
    rows = []
    for chunk in str(entries).split("|"):
        parts = chunk.split("/")
        if len(parts) < 7:
            continue
        rows.append(
            {
                "index": to_int(parts[0]),
                "time": parts[1],
                "price": to_float(parts[2]),
                "change": to_float(parts[3]),
                "volume": to_int(parts[4]),
                "amount": to_float(parts[5]),
                "side": "buy" if parts[6] == "B" else "sell",
            }
        )
    return ok({"code": normalize_code(code), "ticks": rows}, "tencent", extra={"count": len(rows)})


@tool("baidu_kline_with_ma", layer="quotes", ttl="semi_static", cache_domain="kline")
def baidu_kline_with_ma(code: str, period: str = "day", limit: int = 100) -> dict[str, Any]:
    """K-line with moving averages from Baidu (secondary source)."""
    c = code_or_raise(code)
    params = {
        "srcid": "5353",
        "pointType": "string",
        "group": "quotation_kline_ab",
        "query": c,
        "code": c,
        "market_type": "ab",
        "newFormat": "1",
        "is_kc": "0",
        "ktype": period or "day",
        "finClientType": "pc",
    }
    try:
        data = request("baidu", "GET", _Baidu, params=params)
    except TickHubError as exc:
        return _kline_ma_fallback(c, period, limit, f"baidu unavailable: {exc.message}")
    result = (data or {}).get("Result") or {}
    if str(result.get("code")) != "0":
        return _kline_ma_fallback(c, period, limit, f"baidu risk control: {result.get('msg') or result.get('code') or 'blocked'}")
    market = result.get("marketData") or {}
    raw = market.get("klines") or result.get("klines") or []
    rows = []
    for line in raw:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        rows.append(
            {
                "date": parts[0],
                "open": to_float(parts[1]),
                "close": to_float(parts[2]),
                "high": to_float(parts[3]),
                "low": to_float(parts[4]),
                "volume": to_float(parts[5]),
                "ma5": to_float(parts[6]) if len(parts) > 6 else None,
                "ma10": to_float(parts[7]) if len(parts) > 7 else None,
                "ma20": to_float(parts[8]) if len(parts) > 8 else None,
            }
        )
    if not rows:
        return _kline_ma_fallback(c, period, limit, "baidu returned no klines")
    rows = rows[-max(1, int(limit or 100)) :]
    return ok({"code": c, "period": period, "klines": rows}, "baidu", extra={"count": len(rows)})


def _kline_ma_fallback(c: str, period: str, limit: int, note: str) -> dict[str, Any]:
    """Derive MA5/10/20/60 from Tencent closes when Baidu is unreachable."""
    size = max(1, int(limit or 100))
    payload = tencent_kline(c, period=period, limit=min(size + 60, 2000))
    rows = (payload.get("data") or {}).get("klines") or []
    if not rows:
        return empty("baidu", note, code=c)
    closes = [row.get("close") for row in rows]
    for idx, row in enumerate(rows):
        for window in (5, 10, 20, 60):
            window_closes = closes[idx - window + 1 : idx + 1]
            if idx + 1 >= window and all(v is not None for v in window_closes):
                row[f"ma{window}"] = round(sum(window_closes) / window, 3)
            else:
                row[f"ma{window}"] = None
    rows = rows[-size:]
    return ok(
        {"code": c, "period": period, "klines": rows},
        "tencent",
        fallback_from="baidu",
        extra={"count": len(rows), "note": note},
    )


@tool("sina_adjust_factor", layer="quotes", ttl="static", cache_domain="factor")
def sina_adjust_factor(code: str) -> dict[str, Any]:
    """Forward-adjustment factor history from Sina."""
    sym = _symbol(code)
    text = request("sina", "GET", _SINA_QFQ.format(sym=sym), expect_json=False)
    data = js_json(text)
    rows = []
    for item in (data or {}).get("data") or []:
        rows.append({"date": item.get("d"), "factor": to_float(item.get("f"))})
    rows.sort(key=lambda r: str(r["date"]))
    return ok({"code": normalize_code(code), "factors": rows}, "sina", extra={"count": len(rows)})


@tool("apply_adjust", layer="quotes", ttl=None)
def apply_adjust(code: str, closes: str, factors: str = "") -> dict[str, Any]:
    """Apply forward-adjustment factors to raw closes (rule-based, no inference)."""
    c = code_or_raise(code)
    close_values = [to_float(x) for x in str(closes).split(",") if str(x).strip()]
    if not factors:
        factor_rows = sina_adjust_factor(c)["data"]["factors"]
        factor = factor_rows[-1]["factor"] if factor_rows else 1.0
        adjusted = [None if v is None else round(v * float(factor or 1.0), 4) for v in close_values]
        return ok({"code": c, "factor": factor, "adjusted": adjusted}, "computed")
    pairs = [p for p in factors.split(",") if p.strip()]
    if len(pairs) != len(close_values):
        raise TickHubError("QFSD-9001", "factors and closes must have the same length")
    adjusted = [None if v is None else round(v * float(f), 4) for v, f in zip(close_values, pairs)]
    return ok({"code": c, "adjusted": adjusted}, "computed")


@tool("index_quote", layer="quotes", ttl="realtime", cache_domain="quote")
def index_quote(codes: str) -> dict[str, Any]:
    """Realtime index quote (SH/SZ/BSE indices)."""
    rows = _quote_rows(codes)
    for row in rows:
        row["is_index"] = True
    return ok(rows, "tencent", extra={"count": len(rows)})


@tool("etf_quote", layer="quotes", ttl="realtime", cache_domain="quote")
def etf_quote(codes: str) -> dict[str, Any]:
    """Realtime ETF quote (price, volume, turnover, premium inputs)."""
    rows = _quote_rows(codes)
    for row in rows:
        row["is_etf"] = True
    return envelope_rows(rows, "tencent")


def _parse_day_record(blob: bytes) -> dict[str, Any]:
    date, open_, high, low, close, amount, volume, _ = struct.unpack("<IIIIIfII", blob)
    return {
        "date": f"{date // 10000:04d}-{date // 100 % 100:02d}-{date % 100:02d}",
        "open": round(open_ / 100, 2),
        "high": round(high / 100, 2),
        "low": round(low / 100, 2),
        "close": round(close / 100, 2),
        "amount": round(float(amount), 2),
        "volume": int(volume),
    }


def _download_tdx(market: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{market}lday.zip"
    stamp = target.with_suffix(".stamp")
    fresh = target.exists() and stamp.exists() and (_dt.date.today() - _dt.date.fromtimestamp(stamp.stat().st_mtime)).days < 1
    if fresh:
        return target
    resp = request("exchange", "GET", _TDX.format(market=market), expect_json=False, timeout=180, retries=1)
    target.write_bytes(resp.encode("latin-1") if isinstance(resp, str) else resp)
    stamp.write_text(_dt.datetime.now().isoformat(), encoding="utf-8")
    return target


@tool("tdx_daily_package", layer="quotes", ttl="semi_static", cache_domain="tdx")
def tdx_daily_package(date: str = "", market: str = "all", download_dir: str = "") -> dict[str, Any]:
    """Whole-market daily bars for one trading day from the TDX after-hours package."""
    trade_date = require_date(date or None, None)
    markets = ["sh", "sz", "bj"] if market in ("all", "") else [market.lower()]
    cache_dir = Path(download_dir or "./data/tdx")
    out: list[dict[str, Any]] = []
    for mkt in markets:
        if mkt not in {"sh", "sz", "bj"}:
            raise TickHubError("QFSD-9001", f"unknown market: {mkt}")
        path = _download_tdx(mkt, cache_dir)
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/").split("/")[-1]
                if not name.endswith(".day"):
                    continue
                code = name[:-4]
                if not (len(code) == 8 and code[:2] in {"sh", "sz", "bj"}):
                    continue
                if code[:2] != mkt:
                    continue
                blob = zf.read(info)
                if len(blob) < 32:
                    continue
                rec = _parse_day_record(blob[-32:])
                if trade_date and rec["date"] != trade_date:
                    continue
                rec["code"] = code[2:]
                rec["market"] = mkt.upper()
                out.append(
                    {
                        "code": rec["code"],
                        "market": rec["market"],
                        "date": rec["date"],
                        "open": rec["open"],
                        "high": rec["high"],
                        "low": rec["low"],
                        "close": rec["close"],
                        "amount": rec["amount"],
                        "volume": rec["volume"],
                    }
                )
    out.sort(key=lambda r: r["amount"] or 0, reverse=True)
    return ok({"date": trade_date, "rows": out}, "exchange", extra={"count": len(out)})


@tool("tencent_market_snapshot", layer="quotes", ttl="realtime", cache_domain="quote")
def tencent_market_snapshot(codes: str) -> dict[str, Any]:
    """Snapshot for indices/ETFs/stocks in one call."""
    return envelope_rows(_quote_rows(codes), "tencent")


def quote_batch(codes: list[str], *, max_codes: int = 50, concurrency: int = 4) -> dict[str, Any]:
    return run_batch(codes, lambda c: tencent_quote(c), max_codes=max_codes, concurrency=concurrency)


__all__ = [
    "apply_adjust",
    "baidu_kline_with_ma",
    "etf_quote",
    "index_quote",
    "quote_batch",
    "sina_adjust_factor",
    "tdx_daily_package",
    "tencent_kline",
    "tencent_market_snapshot",
    "tencent_minute",
    "tencent_quote",
    "tencent_quote_batch",
    "tencent_ticks",
]
