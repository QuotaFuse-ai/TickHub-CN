"""Research reports, consensus estimates and NL search."""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.config import get_settings
from ..core.errors import TickHubError
from ..core.http import request
from ..core.ticker import normalize_code
from .base import tool
from .util import code_or_raise, empty, ok, to_float, to_int

_REPORT_API = "https://reportapi.eastmoney.com/report/list"
_PDF = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"
_SINA_LIST = "https://vip.stock.finance.sina.com.cn/q/go.php/vReport_List/kind/search/index.phtml"

# The report API filters industries by its own numeric id (447, 448, ...); a
# board code such as BK0475 is silently answered with hits=0.
_INDUSTRY_ID_RE = re.compile(r"^\d{1,4}$")


def _industry_param(industry_code: str) -> tuple[str, str | None]:
    code = str(industry_code or "").strip()
    if not code or code == "*" or _INDUSTRY_ID_RE.match(code):
        return code or "*", None
    return "*", f"东财研报接口按数字行业编号筛选（如 447），{code} 无法作为行业条件；已按全市场检索"


def _reports(*, code: str = "", industry_code: str = "", page: int = 1, page_size: int = 20) -> list[dict[str, Any]]:
    params = {
        "industryCode": industry_code or "*",
        "industry": "*",
        "rating": "",
        "ratingChange": "",
        "beginTime": "",
        "endTime": "",
        "pageSize": str(max(1, min(int(page_size or 20), 100))),
        "pageNo": str(max(1, int(page or 1))),
        "fields": "",
        "qType": "0",
        "orgCode": "",
        "code": code,
        "rcode": "",
    }
    data = request(
        "eastmoney",
        "GET",
        _REPORT_API,
        params=params,
        headers={"Referer": "https://data.eastmoney.com/report/"},
    )
    rows = []
    for item in (data or {}).get("data") or []:
        info = str(item.get("infoCode") or "")
        rows.append(
            {
                "title": item.get("title"),
                "code": item.get("stockCode"),
                "name": item.get("stockName"),
                "org": item.get("orgSName") or item.get("orgName"),
                "analyst": item.get("researcher"),
                "rating": item.get("emRatingName"),
                "publish_date": str(item.get("publishDate") or "")[:10],
                "info_code": info,
                "pdf": _PDF.format(info_code=info) if info else None,
                "eps_next_year": to_float(item.get("predictNextYearEps")),
                "pe_next_year": to_float(item.get("predictNextYearPe")),
            }
        )
    return rows


@tool("eastmoney_reports", layer="research", ttl="semi_static", cache_domain="research")
def eastmoney_reports(code: str = "", industry_code: str = "", page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """Broker research reports with rating and PDF links (Eastmoney)."""
    c = code_or_raise(code) if code else ""
    upstream, note = _industry_param(industry_code)
    rows = _reports(code=c, industry_code=upstream, page=page, page_size=page_size)
    payload = ok({"code": c or None, "industry_code": industry_code or None, "reports": rows, "page": page}, "eastmoney", extra={"count": len(rows)})
    if note:
        payload["meta"]["note"] = note
    return payload


@tool("eastmoney_industry_reports", layer="research", ttl="semi_static", cache_domain="research")
def eastmoney_industry_reports(industry_code: str, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """Industry research reports by Eastmoney industry id (numeric, e.g. 447)."""
    if not str(industry_code or "").strip():
        raise TickHubError("QFSD-9001", "industry_code is required")
    upstream, note = _industry_param(industry_code)
    if note:
        return empty("eastmoney", note, industry_code=industry_code)
    rows = _reports(industry_code=upstream, page=page, page_size=page_size)
    if not rows:
        return empty("eastmoney", f"行业 {upstream} 暂无研报", industry_code=upstream)
    return ok({"industry_code": industry_code, "reports": rows, "page": page}, "eastmoney", extra={"count": len(rows)})


@tool("eastmoney_report_pdf", layer="research", ttl="static", cache_domain="research")
def eastmoney_report_pdf(info_code: str) -> dict[str, Any]:
    """Resolve the PDF download URL for an Eastmoney report."""
    if not info_code:
        raise TickHubError("QFSD-9001", "info_code is required")
    return ok({"info_code": info_code, "pdf": _PDF.format(info_code=info_code)}, "eastmoney")


_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@tool("sina_research_reports", layer="research", ttl="semi_static", cache_domain="research")
def sina_research_reports(code: str = "", page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """Research report list from Sina (secondary source for Eastmoney outages)."""
    c = code_or_raise(code) if code else ""
    params = {"symbol": c, "t1": "all", "p": str(max(1, int(page or 1)))}
    html = request("sina", "GET", _SINA_LIST, params=params, expect_json=False, encoding="gbk")
    rows: list[dict[str, Any]] = []
    for row_html in _ROW_RE.findall(str(html)):
        if "list_table" in row_html:
            continue
        cells = [_TAG_RE.sub("", c).strip() for c in _CELL_RE.findall(row_html)]
        if len(cells) < 4:
            continue
        href_match = re.search(r'href="([^"]+)"', row_html)
        date_cell = next((c for c in cells if _DATE_RE.search(c)), "")
        title = cells[1] if len(cells) > 1 else cells[0]
        if not title or not date_cell:
            continue
        rows.append(
            {
                "title": title,
                "type": cells[2] if len(cells) > 2 else None,
                "org": cells[3] if len(cells) > 3 else None,
                "analyst": cells[4] if len(cells) > 4 else None,
                "publish_date": _DATE_RE.search(date_cell).group(0),
                "url": href_match.group(1) if href_match else None,
            }
        )
        if len(rows) >= max(1, int(page_size or 20)):
            break
    return ok({"code": c or None, "reports": rows, "page": page}, "sina", extra={"count": len(rows)})


_THS_WORTH = "https://basic.10jqka.com.cn/{code}/worth.html"
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_YJYC_RE = re.compile(r'id="yjycData"[^>]*>\s*(\[.*?\])\s*</div>', re.S)
_TIP_RE = re.compile(
    r"截至\s*(\d{4}-\d{2}-\d{2})[^<]{0,40}?共有\s*<strong>(\d+)</strong>\s*家机构",
    re.S,
)
_RATING_RE = re.compile(r'profit-forecast-tab-count-\d">(\d+)</span>')
_RATING_KEYS = ("buy", "overweight", "neutral", "underweight", "sell")


def _cell_text(html: str) -> str:
    return re.sub(r"\s+", "", _TAG_RE.sub("", html)).replace("&nbsp;", "")


def _section(html: str, section_id: str) -> str:
    start = html.find(f'id="{section_id}"')
    if start < 0:
        return ""
    nxt = html.find('<div class="m_box', start + 1)
    return html[start : nxt if nxt > 0 else len(html)]


def _first_table(section: str) -> str:
    start = section.find("<table")
    if start < 0:
        return ""
    end = section.find("</table>", start)
    return section[start : end if end > 0 else len(section)]


def _parse_consensus_table(section: str) -> list[dict[str, Any]]:
    table = _first_table(section)
    if not table:
        return []
    body = table[table.find("<tbody") :] if "<tbody" in table else table
    rows: list[dict[str, Any]] = []
    for row_html in _TR_RE.findall(body):
        cells = [_cell_text(c) for c in _CELL_RE.findall(row_html)]
        if len(cells) < 6 or not re.fullmatch(r"\d{4}", cells[0]):
            continue
        rows.append(
            {
                "year": int(cells[0]),
                "analyst_count": to_int(cells[1]),
                "min": to_float(cells[2]),
                "mean": to_float(cells[3]),
                "max": to_float(cells[4]),
                "industry_avg": to_float(cells[5]),
            }
        )
    return rows


def _parse_history(html: str) -> list[dict[str, Any]]:
    match = _YJYC_RE.search(html)
    if not match:
        return []
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    out = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, list) or len(item) < 3:
            continue
        out.append(
            {
                "year": to_int(item[0]),
                "eps": to_float(item[1]),
                "net_profit_yi": to_float(item[2]),
                "status": "forecast" if str(item[3] if len(item) > 3 else "").upper() == "YC" else "actual",
            }
        )
    return out


def ths_consensus(code: str) -> dict[str, Any]:
    """Broker consensus for one stock from the Tonghuashun F10 forecast page.

    The page renders the consensus table server-side, so a plain HTML fetch is
    enough: no pandas, no JS engine. ``mean`` of the nearest forecast year is
    the consensus EPS used by :func:`~tickhub_cn.datasources.valuation.full_valuation`.
    """
    c = code_or_raise(code)
    url = _THS_WORTH.format(code=c)
    html = str(
        request(
            "ths",
            "GET",
            url,
            expect_json=False,
            encoding="gbk",
            headers={"Referer": "https://basic.10jqka.com.cn/"},
        )
    )
    name_match = _H1_RE.search(html)
    name = _cell_text(name_match.group(1)) if name_match else None
    consensus = _parse_consensus_table(_section(html, "forecast"))
    tip = _TIP_RE.search(html)
    ratings_match = _RATING_RE.findall(_section(html, "stockreport"))
    ratings = {
        key: to_int(value, 0)
        for key, value in zip(_RATING_KEYS, ratings_match + [""] * len(_RATING_KEYS))
        if value != ""
    }
    current = consensus[0] if consensus else None
    nxt = consensus[1] if len(consensus) > 1 else None
    return {
        "code": c,
        "name": name,
        "as_of": tip.group(1) if tip else None,
        "consensus": consensus,
        "current_year": current["year"] if current else None,
        "eps_current_year": current["mean"] if current else None,
        "eps_next_year": nxt["mean"] if nxt else None,
        "analyst_count": current["analyst_count"] if current else (to_int(tip.group(2)) if tip else None),
        "ratings": ratings,
        "history": _parse_history(html),
        "source_url": url,
    }


@tool("ths_eps_forecast", layer="research", ttl="semi_static", cache_domain="research")
def ths_eps_forecast(code: str) -> dict[str, Any]:
    """Institutional consensus EPS by year, with analyst counts and ratings (THS)."""
    c = code_or_raise(code)
    try:
        payload = ths_consensus(c)
    except TickHubError as exc:
        return empty("ths", f"ths eps forecast unavailable: {exc.message}", code=c)
    if not payload["consensus"]:
        return empty(
            "ths",
            "无机构一致预期（部分次新股/北交所标的暂无覆盖）",
            code=c,
            name=payload.get("name"),
            source_url=payload["source_url"],
        )
    out = dict(payload)
    out["eps"] = [row["mean"] for row in payload["consensus"]]
    out["years"] = [str(row["year"]) for row in payload["consensus"]]
    return ok(out, "ths", extra={"count": len(payload["consensus"])}, source_url=payload["source_url"])


@tool("iwencai_search", layer="research", ttl=None)
def iwencai_search(query: str, limit: int = 20) -> dict[str, Any]:
    """Natural-language research search via iwencai (requires API key)."""
    settings = get_settings()
    if not settings.iwencai_api_key:
        raise TickHubError("QFSD-9004", "IWENCAI_API_KEY is not configured")
    payload = {"question": query, "perpage": max(1, min(int(limit or 20), 50)), "page": 1}
    data = request(
        "iwencai",
        "POST",
        settings.iwencai_base_url.rstrip("/") + "/v1/query",
        data=payload,
        headers={
            "Authorization": f"Bearer {settings.iwencai_api_key}",
            "X-Claw-Skill-Id": "tickhub-cn",
            "Content-Type": "application/json",
        },
    )
    rows = (data or {}).get("data") or (data or {}).get("answer") or []
    return ok({"query": query, "results": rows}, "iwencai", extra={"count": len(rows) if isinstance(rows, list) else 0})


__all__ = [
    "eastmoney_industry_reports",
    "eastmoney_report_pdf",
    "eastmoney_reports",
    "iwencai_search",
    "sina_research_reports",
    "ths_eps_forecast",
]
