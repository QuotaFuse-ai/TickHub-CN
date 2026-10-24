"""Convertible bonds: terms plus the latest conversion metrics."""

from __future__ import annotations

from typing import Any

from ..core.errors import TickHubError
from .base import tool
from .emdc import DATACENTER, day, num, strict_rows
from .util import frame, now_shanghai

_CB_QUOTES = (
    "f2~01~CONVERT_STOCK_CODE~CONVERT_STOCK_PRICE,f235~10~SECURITY_CODE~TRANSFER_PRICE,"
    "f236~10~SECURITY_CODE~TRANSFER_VALUE,f2~10~SECURITY_CODE~CURRENT_BOND_PRICE,"
    "f237~10~SECURITY_CODE~TRANSFER_PREMIUM_RATIO"
)


@tool(layer="bond", ttl="semi_static", cache_domain="bond")
def convertible_bonds(include_delisted: bool = False) -> dict[str, Any]:
    """Whole-market convertible bond table (可转债): terms plus live conversion data.

    ``status`` is ``upcoming`` / ``listed`` / ``delisted`` / ``unknown``;
    unknown is reported rather than guessed. Quote fields are filled by the
    upstream latest tick, so suspended or not-yet-listed bonds are None.
    Conversion value = 100 / conversion price * stock price; the premium is
    relative to that value. Issue size is in 100 million CNY.
    """
    rows = strict_rows(
        "RPT_BOND_CB_LIST",
        "",
        "PUBLIC_START_DATE,SECURITY_CODE",
        "-1,1",
        20000,
        narrowed=False,
        extra={"quoteColumns": _CB_QUOTES, "quoteType": "0"},
    )
    if not rows:
        raise TickHubError("QFSD-9003", "东财可转债列表为空，接口可能已变更")
    today = now_shanghai().date().isoformat()
    out = []
    for r in rows:
        listing, delist = day(r.get("LISTING_DATE")), day(r.get("DELIST_DATE"))
        market = r.get("TRADE_MARKET")
        # Eastmoney fills future delist dates once redemption is announced and can
        # prefill listing dates; compare against today instead of trusting presence.
        if (delist and delist <= today) or market == "STAS00":
            status = "delisted"
        elif market not in ("CNSESH", "CNSESZ"):
            status = "unknown"
        elif listing and listing <= today:
            status = "listed"
        elif listing or r.get("PUBLIC_START_DATE"):
            status = "upcoming"
        else:
            status = "unknown"
        if status == "delisted" and not include_delisted:
            continue
        out.append(
            {
                "code": r["SECURITY_CODE"],
                "name": r.get("SECURITY_NAME_ABBR"),
                "status": status,
                "stock_code": r.get("CONVERT_STOCK_CODE"),
                "stock_name": r.get("SECURITY_SHORT_NAME"),
                "rating": r.get("RATING"),
                "issue_size_100m": num(r.get("ACTUAL_ISSUE_SCALE")),
                "apply_date": day(r.get("PUBLIC_START_DATE")),
                "apply_code": r.get("CORRECODE"),
                "listing_date": listing,
                "delist_date": delist,
                "expire_date": day(r.get("EXPIRE_DATE")),
                "convert_start": day(r.get("TRANSFER_START_DATE")),
                "initial_convert_price": num(r.get("INITIAL_TRANSFER_PRICE")),
                "convert_price": num(r.get("TRANSFER_PRICE")),
                "bond_price": num(r.get("CURRENT_BOND_PRICE")),
                "stock_price": num(r.get("CONVERT_STOCK_PRICE")),
                "convert_value": num(r.get("TRANSFER_VALUE")),
                "premium_pct": num(r.get("TRANSFER_PREMIUM_RATIO")),
            }
        )
    codes = [row["code"] for row in out]
    if len(set(codes)) != len(codes):
        raise TickHubError("QFSD-9003", "东财可转债列表代码重复")
    return frame(out, "eastmoney", source_url=f"{DATACENTER}?reportName=RPT_BOND_CB_LIST")
