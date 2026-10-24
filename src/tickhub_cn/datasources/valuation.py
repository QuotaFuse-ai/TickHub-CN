"""Forward valuation: consensus EPS, forward PE, PEG and PE-digestion horizon.

All numbers come from rule-based parsing of the quote feed and the THS
consensus table; no model inference is involved anywhere in this module.
"""

from __future__ import annotations

import math
from typing import Any

from .base import tool
from .quotes import _quote_rows
from .research import ths_consensus
from .util import code_or_raise, empty, ok

DEFAULT_TARGET_PE = 30.0


def forward_pe(price: float, eps_forecast: float) -> float:
    """Price divided by consensus EPS; ``inf`` when the forecast is non-positive."""
    if eps_forecast is None or eps_forecast <= 0:
        return float("inf")
    return price / eps_forecast


def pe_digestion(current_pe: float, cagr: float, target_pe: float = DEFAULT_TARGET_PE) -> float:
    """Years for earnings growth to pull the PE down to ``target_pe``.

    ``0`` when already below target, ``inf`` when the multiple exceeds the
    target but growth is non-positive (it never converges).
    """
    if current_pe is None or not math.isfinite(current_pe):
        return float("inf")
    if current_pe <= target_pe:
        return 0.0
    if cagr is None or cagr <= 0:
        return float("inf")
    return math.log(current_pe / target_pe) / math.log(1 + cagr)


def calc_peg(pe: float, cagr: float) -> float:
    """PEG where ``cagr`` is a decimal fraction (0.15 == 15%)."""
    if pe is None or not math.isfinite(pe) or cagr is None or cagr <= 0:
        return float("inf")
    return pe / (cagr * 100)


def _finite(value: float | None, digits: int) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


@tool("full_valuation", layer="valuation", ttl="realtime", cache_domain="valuation")
def full_valuation(code: str, target_pe: float = DEFAULT_TARGET_PE) -> dict[str, Any]:
    """Forward PE / PEG / PE-digestion from consensus EPS plus live quote."""
    c = code_or_raise(code)
    rows = _quote_rows(c)
    if not rows:
        return empty("tencent", "quote unavailable", code=c)
    quote = rows[0]
    price = quote.get("price") or quote.get("prev_close")
    if not price:
        return empty("tencent", "no tradable price (suspended?)", code=c, name=quote.get("name"))

    try:
        consensus = ths_consensus(c)
    except Exception:  # consensus is an input, not a hard dependency
        consensus = None
    eps_cur = consensus.get("eps_current_year") if consensus else None
    eps_next = consensus.get("eps_next_year") if consensus else None
    analyst_count = consensus.get("analyst_count") if consensus else None

    pe_ttm = quote.get("pe")
    pe_fwd = forward_pe(price, eps_cur) if eps_cur else float("inf")
    cagr = (eps_next / eps_cur - 1) if (eps_cur and eps_cur > 0 and eps_next) else 0.0
    peg = calc_peg(pe_fwd, cagr)
    digest = pe_digestion(pe_fwd, cagr, target_pe) if eps_cur else float("inf")

    return ok(
        {
            "code": quote.get("code") or c,
            "name": quote.get("name"),
            "price": price,
            "mcap_yi": quote.get("total_market_cap"),
            "float_mcap_yi": quote.get("float_market_cap"),
            "pe_ttm": pe_ttm,
            "pb": quote.get("pb"),
            "eps_cur": eps_cur,
            "eps_next": eps_next,
            "eps_cur_year": consensus.get("current_year") if consensus else None,
            "pe_fwd": _finite(pe_fwd, 2),
            "cagr_pct": round(cagr * 100, 1) if cagr else None,
            "peg": _finite(peg, 2),
            "digest_years": _finite(digest, 1),
            "target_pe": target_pe,
            "analyst_count": analyst_count,
            "ratings": consensus.get("ratings") if consensus else {},
            "consensus_as_of": consensus.get("as_of") if consensus else None,
            "notes": None if eps_cur else "无机构一致预期，仅返回静态估值指标",
        },
        "tencent",
        source_url="https://qt.gtimg.cn/q=" + c,
        extra={"consensus_source": "ths", "quote_source": "tencent"},
    )


__all__ = ["DEFAULT_TARGET_PE", "calc_peg", "forward_pe", "full_valuation", "pe_digestion"]
