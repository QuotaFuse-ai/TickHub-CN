"""Company event data: forecasts, surveys, holder trades, buybacks, pledges, IPOs."""

from __future__ import annotations

import datetime as _dt
from typing import Any

from ..core.errors import TickHubError
from .base import tool
from .emdc import DATACENTER, day as _day, limit_value as _limit, num as _num, strict_rows as _event_rows
from .util import frame, now_shanghai, ok, require_date, stock_code

_BUYBACK_PROGRESS = {
    "001": "董事会预案",
    "002": "股东大会通过",
    "003": "股东大会否决",
    "004": "实施中",
    "005": "停止实施",
    "006": "完成实施",
}


def _event_filter(code: str | None, date_field: str | None = None, start: str = "", end: str = "", extra: str = "") -> tuple[str, str, str]:
    """Build the datacenter filter; returns (filter, start, end) with dates normalised."""
    lo = require_date(start) if start else ""
    hi = require_date(end) if end else ""
    if lo and hi and lo > hi:
        raise TickHubError("QFSD-9001", "start 不能晚于 end")
    parts = [extra] if extra else []
    if code:
        parts.append(f'(SECURITY_CODE="{stock_code(code)}")')
    if lo:
        parts.append(f"({date_field}>='{lo}')")
    if hi:
        parts.append(f"({date_field}<='{hi}')")
    return "".join(parts), lo, hi


@tool(layer="event", ttl="semi_static", cache_domain="event")
def earnings_forecast(code: str = "", report_date: str = "", limit: int = 500) -> dict[str, Any]:
    """Earnings previews (业绩预告) for the whole market or one stock.

    One preview is split into several rows by indicator (net profit, ex-items
    profit, revenue...). Amounts in CNY, changes are year-on-year percent.
    """
    limit = _limit(limit)
    period = require_date(report_date) if report_date else ""
    extra = f"(REPORT_DATE='{period}')" if period else ""
    filter_str, _, _ = _event_filter(code, extra=extra)
    rows = _event_rows(
        "RPT_PUBLIC_OP_NEWPREDICT",
        filter_str,
        "NOTICE_DATE,SECURITY_CODE,REPORT_DATE,PREDICT_FINANCE_CODE",
        "-1,1,-1,1",
        limit,
        narrowed=bool(code or period),
        equal={"SECURITY_CODE": stock_code(code)} if code else {},
        dates={"REPORT_DATE": (period, period)} if period else None,
    )
    out = [
        {
            "code": r["SECURITY_CODE"],
            "name": r.get("SECURITY_NAME_ABBR"),
            "notice_date": _day(r.get("NOTICE_DATE")),
            "report_date": _day(r.get("REPORT_DATE")),
            "indicator": r.get("PREDICT_FINANCE"),
            "forecast_type": r.get("PREDICT_TYPE"),
            "amount_lower": _num(r.get("PREDICT_AMT_LOWER")),
            "amount_upper": _num(r.get("PREDICT_AMT_UPPER")),
            "change_pct_lower": _num(r.get("ADD_AMP_LOWER")),
            "change_pct_upper": _num(r.get("ADD_AMP_UPPER")),
            "prior_year_amount": _num(r.get("PREYEAR_SAME_PERIOD")),
            "content": r.get("PREDICT_CONTENT"),
            "reason": r.get("CHANGE_REASON_EXPLAIN"),
        }
        for r in rows
    ]
    return frame(out, "eastmoney", source_url=f"{DATACENTER}?reportName=RPT_PUBLIC_OP_NEWPREDICT")


@tool(layer="event", ttl="semi_static", cache_domain="event")
def institution_survey(
    code: str = "",
    start: str = "",
    end: str = "",
    detail: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    """Institution surveys (机构调研) aggregated from company IR records.

    ``detail=False``: one row per survey with the number of organisations.
    ``detail=True``: one row per organisation.
    ``start``/``end`` filter the announcement date; the actual visit day is
    ``survey_date`` and is usually a few days earlier.
    """
    limit = _limit(limit)
    extra = '(IS_SOURCE="1")' + ("" if detail else '(NUMBERNEW="1")')
    filter_str, lo, hi = _event_filter(code, "NOTICE_DATE", start, end, extra)
    sort = (
        ("NOTICE_DATE,SECURITY_CODE,RECEIVE_START_DATE,NUMBERNEW", "-1,1,-1,1")
        if detail
        else ("NOTICE_DATE,SECURITY_CODE,RECEIVE_START_DATE", "-1,1,-1")
    )
    equal: dict[str, Any] = {"IS_SOURCE": "1"}
    if not detail:
        equal["NUMBERNEW"] = "1"
    if code:
        equal["SECURITY_CODE"] = stock_code(code)
    rows = _event_rows(
        "RPT_ORG_SURVEYNEW",
        filter_str,
        sort[0],
        sort[1],
        limit,
        narrowed=bool(code or lo or hi),
        equal=equal,
        dates={"NOTICE_DATE": (lo, hi)} if (lo or hi) else None,
    )
    out = []
    for r in rows:
        row = {
            "code": r["SECURITY_CODE"],
            "name": r.get("SECURITY_NAME_ABBR"),
            "notice_date": _day(r.get("NOTICE_DATE")),
            "survey_date": _day(r.get("RECEIVE_START_DATE")),
            "survey_end": _day(r.get("RECEIVE_END_DATE")),
            "org_count": _num(r.get("SUM")),
            "survey_way": r.get("RECEIVE_WAY_EXPLAIN"),
            "place": r.get("RECEIVE_PLACE"),
            "receptionist": r.get("RECEPTIONIST"),
        }
        if detail:
            row.update(
                {
                    "org_name": r.get("RECEIVE_OBJECT"),
                    "org_type": r.get("ORG_TYPE"),
                    "investigators": r.get("INVESTIGATORS"),
                }
            )
        out.append(row)
    return frame(out, "eastmoney", source_url=f"{DATACENTER}?reportName=RPT_ORG_SURVEYNEW")


@tool(layer="event", ttl="semi_static", cache_domain="event")
def holder_trades(
    code: str = "",
    direction: str = "",
    start: str = "",
    end: str = "",
    limit: int = 500,
) -> dict[str, Any]:
    """Major-shareholder increases/decreases (股东增减持).

    Share counts in 10k shares; ``change_shares_10k`` is signed (sales are
    negative). ``*_pct_total`` is of total shares, ``*_pct_float`` of float.
    """
    limit = _limit(limit)
    if direction not in ("", "增持", "减持"):
        raise TickHubError("QFSD-9001", "direction 只能是 '' / '增持' / '减持'")
    extra = f'(DIRECTION="{direction}")' if direction else ""
    filter_str, lo, hi = _event_filter(code, "NOTICE_DATE", start, end, extra)
    equal: dict[str, Any] = {"DIRECTION": direction} if direction else {}
    if code:
        equal["SECURITY_CODE"] = stock_code(code)
    rows = _event_rows(
        "RPT_SHARE_HOLDER_INCREASE",
        filter_str,
        "NOTICE_DATE,SECURITY_CODE,HOLDER_NAME,START_DATE,END_DATE",
        "-1,1,1,1,1",
        limit,
        narrowed=bool(code or lo or hi),
        equal=equal,
        dates={"NOTICE_DATE": (lo, hi)} if (lo or hi) else None,
    )
    out = []
    for r in rows:
        signed = _num(r.get("CHANGE_NUM_SYMBOL"))
        if signed is not None and r.get("DIRECTION") in ("增持", "减持") and (signed < 0) != (r["DIRECTION"] == "减持"):
            raise TickHubError(
                "QFSD-9003",
                f"东财增减持方向与变动股数符号不一致: {r.get('SECURITY_CODE')} {r.get('HOLDER_NAME')}",
            )
        out.append(
            {
                "code": r["SECURITY_CODE"],
                "name": r.get("SECURITY_NAME_ABBR"),
                "holder": r.get("HOLDER_NAME"),
                "direction": r.get("DIRECTION"),
                "change_shares_10k": signed,
                # AFTER_CHANGE_RATE is actually the change as a share of total equity.
                "change_pct_total": _num(r.get("AFTER_CHANGE_RATE")),
                "change_pct_float": _num(r.get("CHANGE_FREE_RATIO")),
                "after_shares_10k": _num(r.get("AFTER_HOLDER_NUM")),
                "after_pct_total": _num(r.get("HOLD_RATIO")),
                "after_float_shares_10k": _num(r.get("FREE_SHARES")),
                "after_pct_float": _num(r.get("FREE_SHARES_RATIO")),
                "avg_price": _num(r.get("TRADE_AVERAGE_PRICE")),
                "channel": r.get("MARKET"),
                "start_date": _day(r.get("START_DATE")),
                "end_date": _day(r.get("END_DATE")),
                "notice_date": _day(r.get("NOTICE_DATE")),
            }
        )
    return frame(out, "eastmoney", source_url=f"{DATACENTER}?reportName=RPT_SHARE_HOLDER_INCREASE")


@tool(layer="event", ttl="semi_static", cache_domain="event")
def share_buyback(code: str = "", progress: str = "", limit: int = 500) -> dict[str, Any]:
    """Share buyback plans and their implementation progress (股票回购).

    One row per plan, newest announcement first. Share counts in shares,
    amounts in CNY, ``pct_total_*`` of total shares before the announcement.
    """
    limit = _limit(limit)
    codes = {v: k for k, v in _BUYBACK_PROGRESS.items()}
    if progress and progress not in codes:
        raise TickHubError("QFSD-9001", "progress 只能是 " + " / ".join(codes))
    equal: dict[str, Any] = {}
    if code:
        equal["DIM_SCODE"] = stock_code(code)  # this report names the code DIM_SCODE
    if progress:
        equal["REPURPROGRESS"] = codes[progress]
    filter_str = "".join(f'({field}="{value}")' for field, value in equal.items())
    rows = _event_rows(
        "RPTA_WEB_GETHGLIST_NEW",
        filter_str,
        "UPD,DIM_SCODE,REPURCODE",
        "-1,1,1",
        limit,
        narrowed=bool(equal),
        equal=equal,
    )
    out = [
        {
            "code": r["DIM_SCODE"],
            "name": r.get("SECURITYSHORTNAME"),
            "progress": _BUYBACK_PROGRESS.get(r.get("REPURPROGRESS")),
            "progress_code": r.get("REPURPROGRESS"),
            "plan_start": _day(r.get("REPURSTARTDATE")),
            "plan_end": _day(r.get("REPURENDDATE")),
            "price_cap": _num(r.get("REPURPRICECAP")),
            "shares_lower": _num(r.get("REPURNUMLOWER")),
            "shares_upper": _num(r.get("REPURNUMCAP")),
            "amount_lower": _num(r.get("REPURAMOUNTLOWER")),
            "amount_upper": _num(r.get("REPURAMOUNTLIMIT")),
            "pct_total_lower": _num(r.get("ZSZXX")),
            "pct_total_upper": _num(r.get("ZSZSX")),
            "done_shares": _num(r.get("REPURNUM")),
            "done_amount": _num(r.get("REPURAMOUNT")),
            "done_price_low": _num(r.get("REPURPRICELOWER1")),
            "done_price_high": _num(r.get("REPURPRICECAP1")),
            "latest_notice": _day(r.get("UPDATEDATE")),
            "objective": r.get("REPUROBJECTIVE"),
        }
        for r in rows
    ]
    return frame(out, "eastmoney", source_url=f"{DATACENTER}?reportName=RPTA_WEB_GETHGLIST_NEW")


@tool(layer="event", ttl="semi_static", cache_domain="event")
def equity_pledge(code: str = "", date: str = "", limit: int = 5000) -> dict[str, Any]:
    """Equity pledge ratio (股权质押), ChinaClear weekly statistics via Eastmoney.

    ``code`` gives that stock's history; ``date`` gives the whole market on one
    statistics day; neither gives the latest statistics day. Only Shanghai and
    Shenzhen are covered — Beijing codes raise instead of returning empty.
    """
    limit = _limit(limit)
    if code and date:
        raise TickHubError("QFSD-9001", "code 与 date 只能给一个：code 取该股历次统计，date 取该统计日全市场")
    report = "RPT_CSDC_LIST"
    url = f"{DATACENTER}?reportName={report}"
    if code:
        c = stock_code(code)
        if c.startswith(("4", "8", "92")):
            raise TickHubError("QFSD-9001", f"{code} 是北交所证券；中国结算质押统计只覆盖沪深，没有北交所数据")
        rows = _event_rows(
            report,
            _event_filter(code)[0],
            "TRADE_DATE",
            "-1",
            limit,
            narrowed=True,
            equal={"SECURITY_CODE": c},
        )
    else:
        day = require_date(date) if date else ""
        if not day:
            latest = _event_rows(report, "", "TRADE_DATE", "-1", 1, narrowed=False)
            day = str(latest[0]["TRADE_DATE"])[:10]
        rows = _event_rows(
            report,
            f"(TRADE_DATE='{day}')",
            "PLEDGE_RATIO,SECURITY_CODE",
            "-1,1",
            limit,
            narrowed=True,
            dates={"TRADE_DATE": (day, day)},
        )
        if not rows:
            raise TickHubError("QFSD-9001", f"{day} 不是中国结算质押统计日（按周发布，通常为周五）")
    out = []
    for r in rows:
        total = _num(r.get("REPURCHASE_BALANCE"))
        free = _num(r.get("REPURCHASE_UNLIMITED_BALANCE"))
        locked = _num(r.get("REPURCHASE_LIMITED_BALANCE"))
        if None not in (total, free, locked) and abs(free + locked - total) > max(1.0, total * 0.001):
            raise TickHubError(
                "QFSD-9003",
                f"东财质押数据 无限售 + 限售 ≠ 合计: {r.get('SECURITY_CODE')} {r.get('TRADE_DATE')}",
            )
        out.append(
            {
                "date": _day(r.get("TRADE_DATE")),
                "code": r.get("SECURITY_CODE"),
                "name": r.get("SECURITY_NAME_ABBR"),
                "industry": r.get("INDUSTRY"),
                "pledge_ratio_pct": _num(r.get("PLEDGE_RATIO")),
                "pledged_shares_10k": total,
                "pledged_mktcap_10k": _num(r.get("PLEDGE_MARKET_CAP")),
                "pledge_count": _num(r.get("PLEDGE_DEAL_NUM")),
                "unrestricted_pledged_10k": free,
                "restricted_pledged_10k": locked,
            }
        )
    keys = [(row["date"], row["code"]) for row in out]
    if len(set(keys)) != len(keys):
        raise TickHubError("QFSD-9003", "东财质押数据 日期+代码 重复")
    return frame(out, "eastmoney", source_url=url)


@tool(layer="event", ttl="semi_static", cache_domain="event")
def ipo_calendar(limit: int = 100) -> dict[str, Any]:
    """New-share subscription calendar (新股申购日历), newest subscription first.

    Includes issues that are scheduled but not yet open. ``issue_price`` is
    None before pricing; market cap in 10k CNY, win rate and first-day gain %.
    """
    limit = _limit(limit)
    rows = _event_rows(
        "RPTA_APP_IPOAPPLY",
        "",
        "APPLY_DATE,SECURITY_CODE",
        "-1,-1",
        limit,
        narrowed=False,
    )
    out = [
        {
            "code": r["SECURITY_CODE"],
            "name": r.get("SECURITY_NAME"),
            "apply_code": r.get("APPLY_CODE"),
            "exchange": r.get("TRADE_MARKET"),
            # MARKET names the exact board; MARKET_TYPE_NEW marks not-yet-listed
            # Shenzhen issues as "other", but is the only clue for Beijing.
            "board": r.get("MARKET") or r.get("MARKET_TYPE_NEW"),
            "apply_date": _day(r.get("APPLY_DATE")),
            "ballot_date": _day(r.get("BALLOT_NUM_DATE")),
            "pay_date": _day(r.get("BALLOT_PAY_DATE")),
            "listing_date": _day(r.get("LISTING_DATE")),
            "issue_price": _num(r.get("ISSUE_PRICE")) or None,
            "issue_pe": _num(r.get("AFTER_ISSUE_PE")),
            "industry_pe": _num(r.get("INDUSTRY_PE")),
            "issue_shares_10k": _num(r.get("ISSUE_NUM")),
            "online_shares": _num(r.get("ONLINE_ISSUE_NUM")),
            "apply_upper_shares": _num(r.get("ONLINE_APPLY_UPPER")),
            "top_apply_mktcap_10k": _num(r.get("TOP_APPLY_MARKETCAP")),
            "win_rate_pct": _num(r.get("ONLINE_ISSUE_LWR")),
            "first_close": _num(r.get("CLOSE_PRICE")),
            "first_close_chg_pct": _num(r.get("LD_CLOSE_CHANGE")),
        }
        for r in rows
    ]
    seen = [row["code"] for row in out]
    if len(set(seen)) != len(seen):
        raise TickHubError("QFSD-9003", "东财新股日历代码重复")
    return frame(out, "eastmoney", source_url=f"{DATACENTER}?reportName=RPTA_APP_IPOAPPLY")


@tool(layer="event", ttl="semi_static", cache_domain="event")
def lockup_expiry(code: str, trade_date: str = "", forward_days: int = 90) -> dict[str, Any]:
    """Restricted-share unlock calendar (限售解禁): history plus the coming window.

    ``history`` is the latest 15 unlock batches, ``upcoming`` covers
    ``forward_days`` from ``trade_date`` (today by default). Share counts are in
    10k shares, ``lift_market_cap_10k`` in 10k CNY, ``ratio_pct`` of total shares.
    """
    c = stock_code(code)
    anchor = require_date(trade_date) if trade_date else now_shanghai().date().isoformat()
    horizon = min(max(1, int(forward_days or 90)), 730)
    end = (_dt.date.fromisoformat(anchor) + _dt.timedelta(days=horizon)).isoformat()

    def _lift_rows(
        filter_str: str, sort_types: str, limit: int, dates: dict[str, tuple[str, str]] | None
    ) -> list[dict[str, Any]]:
        return _event_rows(
            "RPT_LIFT_STAGE",
            filter_str,
            "FREE_DATE",
            sort_types,
            limit,
            narrowed=True,
            equal={"SECURITY_CODE": c},
            dates=dates,
        )

    def _shape(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "date": _day(r.get("FREE_DATE")),
                "type": r.get("FREE_SHARES_TYPE") or "",
                "shares_10k": _num(r.get("FREE_SHARES")),
                # ABLE is what can actually trade that day; it lags FREE_SHARES
                # when part of the batch stays locked.
                "able_shares_10k": _num(r.get("ABLE_FREE_SHARES")),
                "ratio_pct": round((_num(r.get("FREE_RATIO")) or 0.0) * 100, 4),
                "lift_market_cap_10k": _num(r.get("LIFT_MARKET_CAP")),
            }
            for r in rows
        ]

    history = _shape(_lift_rows(f'(SECURITY_CODE="{c}")', "-1", 15, None))
    upcoming = _shape(
        _lift_rows(
            f"(SECURITY_CODE=\"{c}\")(FREE_DATE>='{anchor}')(FREE_DATE<='{end}')",
            "1",
            20,
            {"FREE_DATE": (anchor, end)},
        )
    )
    return ok(
        {"code": c, "trade_date": anchor, "forward_days": horizon, "history": history, "upcoming": upcoming},
        "eastmoney",
        extra={"source_url": f"{DATACENTER}?reportName=RPT_LIFT_STAGE", "count": len(history) + len(upcoming)},
    )
