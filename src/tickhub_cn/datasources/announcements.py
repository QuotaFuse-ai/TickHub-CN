"""Announcements, news wires and official backups."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urlparse

from ..core.errors import TickHubError
from ..core.http import em_get, request
from ..core.ticker import compact_date, market_of, normalize_code, parse_date
from ..risk.events import classify_announcement, from_announcements
from .base import tool
from .util import code_or_raise, empty, ok, require_date, to_int

_CNINFO_QUERY = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
_CNINFO_ORG = "http://www.cninfo.com.cn/new/information/topSearch/query"
_CNINFO_PDF = "http://static.cninfo.com.cn/"


def _cninfo_org_id(code: str) -> str:
    data = request("cninfo", "POST", _CNINFO_ORG, data={"keyWord": code, "maxNum": "10"})
    for item in data or []:
        if str(item.get("code")) == code:
            return str(item.get("orgId") or "")
    return ""


def _cninfo_rows(
    code: str,
    start: str,
    end: str,
    category: str,
    page: int,
    page_size: int,
    searchkey: str = "",
) -> tuple[list[dict[str, Any]], int]:
    org_id = _cninfo_org_id(code)
    payload = {
        "pageNum": str(max(1, int(page or 1))),
        "pageSize": str(max(1, min(int(page_size or 30), 100))),
        # Blank lets CNINFO apply its own per-exchange rules: a fixed "szse"
        # silently drops filings that are only classified under the issuer's
        # own board (e.g. investor-relations records on Shenzhen names).
        "column": "",
        "tabName": "fulltext",
        "plate": "",
        # An empty org id makes CNINFO search every issuer, so it must be resolved.
        "stock": f"{code},{org_id}",
        "searchkey": searchkey,
        "secdate": "",
        "tdate": "",
        "type": category or "",
        "category": category or "",
        "trade": "",
        "seDate": f"{start}~{end}" if start or end else "",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    data = request(
        "cninfo",
        "POST",
        _CNINFO_QUERY,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"},
    )
    rows = []
    for item in (data or {}).get("announcements") or []:
        stamp = item.get("announcementTime")
        published = ""
        if isinstance(stamp, (int, float)):
            import datetime as _dt

            published = _dt.datetime.fromtimestamp(stamp / 1000).strftime("%Y-%m-%d %H:%M:%S")
        else:
            published = str(stamp or "")
        adjunct = str(item.get("adjunctUrl") or "")
        rows.append(
            {
                "code": item.get("secCode"),
                "name": item.get("secName"),
                "title": re.sub(r"<[^>]+>", "", str(item.get("announcementTitle") or "")),
                "type": item.get("announcementTypeName"),
                "publish_time": published,
                "url": _CNINFO_PDF + adjunct.lstrip("/") if adjunct else "",
                "pdf": _CNINFO_PDF + adjunct.lstrip("/") if adjunct.endswith(".pdf") else None,
            }
        )
    return rows, to_int((data or {}).get("totalAnnouncement"), len(rows)) or len(rows)


@tool("cninfo_announcements", layer="announcements", ttl="semi_static", cache_domain="announcement")
def cninfo_announcements(code: str, start: str = "", end: str = "", category: str = "", limit: int = 30, page: int = 1) -> dict[str, Any]:
    """CNINFO announcements with PDF links (Shanghai/Shenzhen/Beijing)."""
    c = code_or_raise(code)
    start_date = require_date(start or None, "")
    end_date = require_date(end or None, "")
    rows, total = _cninfo_rows(c, start_date, end_date, category, page, limit)
    return ok({"code": c, "announcements": rows, "total": total, "page": page}, "cninfo", extra={"count": len(rows)})


@tool("cninfo_announcement_risk", layer="announcements", ttl="semi_static", cache_domain="announcement")
def cninfo_announcement_risk(code: str, start: str = "", end: str = "", limit: int = 100) -> dict[str, Any]:
    """Classify recent announcements into structured risk events."""
    c = code_or_raise(code)
    start_date = require_date(start or None, "")
    end_date = require_date(end or None, "")
    rows, _ = _cninfo_rows(c, start_date, end_date, "", 1, limit)
    events = from_announcements(c, rows)
    return ok(
        {"code": c, "announcements": len(rows), "events": events, "total": len(events)},
        "computed",
        extra={"count": len(events)},
    )


@tool("szse_announcements", layer="announcements", ttl="semi_static", cache_domain="announcement")
def szse_announcements(code: str, start: str = "", end: str = "", page: int = 1, page_size: int = 30) -> dict[str, Any]:
    """Shenzhen Stock Exchange official announcements (backup source)."""
    c = code_or_raise(code)
    if market_of(c) != "sz":
        raise TickHubError("QFSD-9002", "SZSE announcements only cover Shenzhen-listed stocks")
    body = {
        "seDate": [require_date(start or None, ""), require_date(end or None, "")],
        "stock": [c],
        "channelCode": ["listedNotice_disc"],
        "pageSize": max(1, min(int(page_size or 30), 100)),
        "pageNum": max(1, int(page or 1)),
    }
    data = request(
        "exchange",
        "POST",
        "https://www.szse.cn/api/disc/announcement/annList",
        data=json.dumps(body),
        headers={"Content-Type": "application/json", "Referer": "https://www.szse.cn/disclosure/listed/notice/index.html"},
    )
    rows = []
    for item in (data or {}).get("data") or []:
        codes = item.get("secCode") or []
        names = item.get("secName") or []
        path = str(item.get("attachPath") or "")
        rows.append(
            {
                "code": codes[0] if isinstance(codes, list) and codes else codes,
                "name": names[0] if isinstance(names, list) and names else names,
                "title": item.get("title"),
                "format": item.get("attachFormat"),
                "size_kb": item.get("attachSize"),
                "publish_time": item.get("publishTime"),
                "url": ("https://disc.static.szse.cn/download" + path) if path else None,
            }
        )
    return ok({"code": c, "announcements": rows}, "szse", extra={"count": len(rows)})


@tool("eastmoney_stock_news", layer="announcements", ttl="realtime", cache_domain="news")
def eastmoney_stock_news(code: str, limit: int = 20) -> dict[str, Any]:
    """Individual stock news from the Eastmoney quote-page feed."""
    c = code_or_raise(code)
    size = max(1, min(int(limit or 20), 100))
    data = em_get(
        "https://np-listapi.eastmoney.com/comm/web/getListInfo",
        params={
            "cfh": "1",
            "client": "web",
            "mTypeAndCode": f"{1 if market_of(c) == 'sh' else 0}.{c}",
            "type": "1",
            "pageSize": str(size),
            "pageIndex": "1",
        },
    )
    body = (data or {}).get("data") or {}
    rows = []
    for item in body.get("list") or []:
        origin = str(item.get("Art_OriginUrl") or "")
        rows.append(
            {
                "date": item.get("Art_ShowTime"),
                "media": urlparse(origin).hostname if origin else None,
                "title": re.sub(r"<[^>]+>", "", str(item.get("Art_Title") or "")),
                "summary": "",
                "url": item.get("Art_Url") or origin or None,
            }
        )
    if not rows:
        return empty("eastmoney", "no stock news published for this code", code=c)
    return ok({"code": c, "news": rows}, "eastmoney", extra={"count": len(rows)})


@tool("cls_telegraph", layer="announcements", ttl="realtime", cache_domain="news")
def cls_telegraph(limit: int = 50) -> dict[str, Any]:
    """Cailianshe (CLS) 7x24 telegraph flashes."""
    query = {"app": "CailianpressWeb", "os": "web", "sv": "8.4.6", "rn": str(max(1, min(int(limit or 50), 100)))}
    raw = "&".join(f"{k}={query[k]}" for k in sorted(query))
    query["sign"] = hashlib.md5(hashlib.sha1(raw.encode()).hexdigest().encode()).hexdigest()
    data = request(
        "cls",
        "GET",
        "https://www.cls.cn/v1/roll/get_roll_list",
        params=query,
        headers={"Referer": "https://www.cls.cn/telegraph"},
    )
    rows = []
    for item in ((data or {}).get("data") or {}).get("roll_data") or []:
        rows.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "content": re.sub(r"<[^>]+>", "", str(item.get("content") or "")),
                "publish_time": item.get("ctime"),
                "level": item.get("level"),
                "url": f"https://www.cls.cn/detail/{item.get('id')}" if item.get("id") else "",
            }
        )
    return ok(rows, "cls", extra={"count": len(rows)})


_WSC_CHANNELS = {
    "a-stock": "a-stock-channel",
    "global": "global-channel",
    "us": "us-stock-channel",
    "hk": "hk-stock-channel",
    "commodity": "commodity-channel",
}


@tool("wallstreetcn_lives", layer="announcements", ttl="realtime", cache_domain="news")
def wallstreetcn_lives(channel: str = "a-stock", limit: int = 50) -> dict[str, Any]:
    """Wallstreetcn 7x24 live feed (A-share / global channels)."""
    key = _WSC_CHANNELS.get(channel, channel)
    data = request(
        "wallstreetcn",
        "GET",
        "https://api-one.wallstcn.com/apiv1/content/lives",
        params={"channel": key, "limit": max(1, min(int(limit or 50), 100))},
        headers={"Referer": "https://wallstreetcn.com/"},
    )
    rows = []
    for item in ((data or {}).get("data") or {}).get("items") or []:
        rows.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "content": re.sub(r"<[^>]+>", "", str(item.get("content_text") or "")),
                "publish_time": item.get("display_time"),
                "score": item.get("score"),
                "uri": item.get("uri"),
            }
        )
    return ok(rows, "wallstreetcn", extra={"count": len(rows)})


@tool("eastmoney_global_news", layer="announcements", ttl="realtime", cache_domain="news")
def eastmoney_global_news(limit: int = 50) -> dict[str, Any]:
    """Eastmoney 7x24 global fast news."""
    data = em_get(
        "https://np-weblist.eastmoney.com/comm/web/getFastNewsList",
        params={
            "client": "web",
            "biz": "web_724",
            "fastColumn": "102",
            "sortEnd": "",
            "pageSize": str(max(1, min(int(limit or 50), 100))),
            # The feed rejects requests without a trace id.
            "req_trace": str(int(time.time() * 1000)),
        },
    )
    rows = []
    for item in ((data or {}).get("data") or {}).get("fastNewsList") or []:
        rows.append(
            {
                "code": item.get("code"),
                "title": item.get("title"),
                "summary": item.get("summary"),
                "publish_time": item.get("showTime"),
            }
        )
    return ok(rows, "eastmoney", extra={"count": len(rows)})


_A_RE = re.compile(r"<a\b[^>]*>", re.S)
_HREF_RE = re.compile(r'href="([^"]+)"')
_TITLE_RE = re.compile(r'title="([^"]+)"')


@tool("cctv_news", layer="announcements", ttl="semi_static", cache_domain="news")
def cctv_news(date: str = "") -> dict[str, Any]:
    """CCTV Xinwen Lianbo item list for one date."""
    day = compact_date(date or None)
    url = f"https://tv.cctv.com/lm/xwlb/day/{day}.shtml"
    html = request("cctv", "GET", url, expect_json=False, encoding="utf-8")
    rows = []
    seen: set[str] = set()
    for tag in _A_RE.findall(str(html)):
        href_match = _HREF_RE.search(tag)
        title_match = _TITLE_RE.search(tag)
        if not href_match or not title_match:
            continue
        href = href_match.group(1)
        text = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
        if not text or href in seen:
            continue
        seen.add(href)
        rows.append({"title": text, "url": href if href.startswith("http") else "https:" + href})
    if not rows:
        return empty("cctv", f"no Xinwen Lianbo items for {day}", date=day)
    return ok({"date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "items": rows}, "cctv", extra={"count": len(rows)})


__all__ = [
    "cctv_news",
    "cls_telegraph",
    "cninfo_announcement_risk",
    "cninfo_announcements",
    "eastmoney_global_news",
    "eastmoney_stock_news",
    "szse_announcements",
    "wallstreetcn_lives",
]
