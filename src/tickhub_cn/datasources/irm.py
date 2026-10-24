"""Investor-relations Q&A (CNINFO 互动易 / SSE e-Interaction) and concept hits."""

from __future__ import annotations

import datetime as _dt
import html as _html
import json
import re
from typing import Any

from ..core.errors import TickHubError
from ..core.http import request
from ..core.ticker import market_of, normalize_code
from .base import tool
from .util import empty, frame, now_shanghai, stock_code, to_float

_CNINFO_SEARCH = "https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo"
_CNINFO_QA = "https://irm.cninfo.com.cn/newircs/company/question"

_SSE_BASE = "https://sns.sseinfo.com"
_SSE_KINDS = {"answered": 11, "questions": 10}
_SSE_HEADERS = {"Referer": _SSE_BASE + "/"}
_SSE_COMPANY_END = "没有任何上市公司的信息"
_SSE_EMPTY_NOTE = re.compile(r"""class=["']m_feed_note["'][^>]*>[^<]*(暂无|暂时没有)[^<]*<""")
_SSE_NOTE_TEXT = re.compile(r"""class=["']m_feed_note["'][^>]*>([^<]*)<""")
_SSE_UID_CACHE: dict[str, str] = {}
_SSE_COMPANY_PAGES: dict[int, list[tuple[str, str]]] = {}

_EM_HOT_CONCEPT = "https://emappdata.eastmoney.com/stockrank/getHotStockRankList"
_EM_APP = {"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38"}


def _cninfo_time(value: Any) -> str:
    stamp = to_float(value)
    if not stamp:
        return ""
    return _dt.datetime.fromtimestamp(stamp / 1000, tz=_dt.timezone(_dt.timedelta(hours=8))).strftime(
        "%Y-%m-%d %H:%M"
    )


@tool("cninfo_irm", layer="sentiment", ttl="realtime", cache_domain="sentiment")
def cninfo_irm(code: str, page_size: int = 30, page_num: int = 1) -> dict[str, Any]:
    """Investor questions and official replies from CNINFO 互动易 (Shenzhen listed)."""
    c = stock_code(code)
    size = max(1, min(int(page_size or 30), 50))
    page = max(1, int(page_num or 1))
    if market_of(c) != "sz":
        return empty(
            "cninfo",
            "互动易只覆盖深市（含创业板）；沪市请用 sse_e_interaction，北交所两个平台均无数据",
        )

    hits = request("cninfo", "POST", _CNINFO_SEARCH, data={"keyWord": c})
    org_id = ""
    for item in (hits or {}).get("data") or []:
        if str(item.get("stockCode") or "") == c:
            org_id = str(item.get("secid") or "")
            break
    if not org_id:
        raise TickHubError("QFSD-9005", f"互动易平台没有 {c}（不在深市或已退市）")

    # The second call only works with its parameters in the query string; a
    # form body makes the endpoint answer HTTP 400.
    payload = request(
        "cninfo",
        "POST",
        _CNINFO_QA,
        params={
            "_t": 1,
            "stockcode": c,
            "orgId": org_id,
            "pageSize": size,
            "pageNum": page,
            "keyWord": "",
            "startDay": "",
            "endDay": "",
        },
        headers={"Referer": "https://irm.cninfo.com.cn/"},
    )
    rows = []
    for item in (payload or {}).get("rows") or []:
        rows.append(
            {
                "code": item.get("stockCode"),
                "company": item.get("companyShortName"),
                "question": item.get("mainContent"),
                "answer": item.get("attachedContent") or None,
                "answerer": item.get("attachedAuthor"),
                "ask_time": _cninfo_time(item.get("pubDate")),
            }
        )
    total = (payload or {}).get("total")
    return frame(
        rows,
        "cninfo",
        source_url=_CNINFO_QA,
        page=page,
        page_size=size,
        total_rows=total,
    )


def _sse_company_page(page: int) -> list[tuple[str, str]]:
    if page not in _SSE_COMPANY_PAGES:
        payload = request(
            "sse",
            "POST",
            _SSE_BASE + "/allcompany.do",
            data={"code": "0", "order": "2", "areaId": "0", "page": page},
            headers=_SSE_HEADERS,
        )
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str):
            raise TickHubError(
                "QFSD-9003",
                f"上证e互动公司列表第 {page} 页返回结构已变（没有 content 字符串）",
            )
        pairs = [
            (code, uid)
            for uid, code in re.findall(
                r"uid=['\"]?(\d+)['\"]?[^>]*>\s*<img[^>]*company/(\d{6})\.png", content
            )
        ]
        # Pages past the end carry a fixed "no companies" sentence. An empty page
        # without it (or an empty first page) means the layout changed.
        if not pairs and (page == 1 or _SSE_COMPANY_END not in content):
            raise TickHubError(
                "QFSD-9003",
                f"上证e互动公司列表第 {page} 页解析出 0 家公司，页面格式可能已变",
            )
        _SSE_COMPANY_PAGES[page] = pairs
        for code, uid in pairs:
            _SSE_UID_CACHE[code] = uid
    return _SSE_COMPANY_PAGES[page]


def _sse_company_uid(code: str) -> str:
    """Locate a company uid in the code-sorted, 32-per-page SSE company list."""
    if code in _SSE_UID_CACHE:
        return _SSE_UID_CACHE[code]
    low = high = 1
    while _sse_company_page(high):
        if _sse_company_page(high)[-1][0] >= code:
            break
        low, high = high, high * 2
    while low <= high:
        mid = (low + high) // 2
        pairs = _sse_company_page(mid)
        if not pairs or code < pairs[0][0]:
            high = mid - 1
        elif code > pairs[-1][0]:
            low = mid + 1
        else:
            break
    if code not in _SSE_UID_CACHE:
        raise TickHubError(
            "QFSD-9005", f"上证e互动没有 {code}（公司不在上交所，或已退市）"
        )
    return _SSE_UID_CACHE[code]


def _sse_text(fragment: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


_SSE_ABS_TIME = re.compile(
    r"(\d{4})\s*[年\-/]\s*(\d{1,2})\s*[月\-/]\s*(\d{1,2})\s*日?\s*(\d{1,2}):(\d{2})"
)
_SSE_DAY_WORD_TIME = re.compile(r"^(今天|昨天|前天)\s*(\d{1,2}):(\d{2})$")
_SSE_MONTH_DAY_TIME = re.compile(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})$")
_SSE_AGO_TIME = re.compile(r"^(\d+)\s*(秒|分钟|小时|天)前$")
_SSE_CLOCK_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
_SSE_DAY_WORD = {"今天": 0, "昨天": 1, "前天": 2}
_SSE_AGO_UNIT = {"秒": "seconds", "分钟": "minutes", "小时": "hours", "天": "days"}


def _sse_time(text: str, now: _dt.datetime | None = None) -> str | None:
    """Normalise the site's mixed absolute/relative stamps to ``YYYY-MM-DD HH:MM``."""
    text = (text or "").strip()
    now = (now or now_shanghai()).replace(tzinfo=None)
    match = _SSE_ABS_TIME.search(text)
    if match:
        year, month, day, hour, minute = (int(group) for group in match.groups())
        try:
            return _dt.datetime(year, month, day, hour, minute).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return None
    match = _SSE_DAY_WORD_TIME.match(text)
    if match:
        when = now - _dt.timedelta(days=_SSE_DAY_WORD[match.group(1)])
        try:
            return when.replace(
                hour=int(match.group(2)), minute=int(match.group(3)), second=0, microsecond=0
            ).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return None
    match = _SSE_MONTH_DAY_TIME.match(text)
    if match:
        month, day, hour, minute = (int(group) for group in match.groups())
        try:
            when = _dt.datetime(now.year, month, day, hour, minute)
        except ValueError:
            return None
        if when - now > _dt.timedelta(days=1):
            try:
                when = when.replace(year=now.year - 1)
            except ValueError:
                return None
        return when.strftime("%Y-%m-%d %H:%M")
    match = _SSE_AGO_TIME.match(text)
    if match:
        try:
            delta = _dt.timedelta(**{_SSE_AGO_UNIT[match.group(2)]: int(match.group(1))})
            return (now - delta).strftime("%Y-%m-%d %H:%M")
        except (OverflowError, ValueError):
            return None
    if text in ("刚刚", "刚才"):
        return now.strftime("%Y-%m-%d %H:%M")
    match = _SSE_CLOCK_TIME.match(text)
    if match:
        try:
            return now.replace(
                hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0
            ).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return None
    return None


def _sse_required_time(item_id: str, text: str) -> str:
    when = _sse_time(text)
    if when is None:
        raise TickHubError(
            "QFSD-9003", f"上证e互动第 {item_id} 条的时间认不出: {text!r}"
        )
    return when


def _sse_parse_feed(text: str) -> list[dict[str, Any]]:
    """Split each feed item at the reply block, since the question block's class differs."""
    rows = []
    for chunk in re.split(r'<div class="m_feed_item[^"]*" id="item-', text)[1:]:
        numbered = re.match(r"(\d+)", chunk)
        if not numbered:
            raise TickHubError(
                "QFSD-9003", f"上证e互动条目 id 不是数字，页面结构可能已变: {chunk[:60]!r}"
            )
        item_id = numbered.group(1)
        ask_part, _, answer_part = chunk.partition('class="m_feed_detail m_qa"')
        question = re.search(
            r'<div class="m_feed_txt"[^>]*>\s*<a[^>]*>:(.*?)\((\d{6})\)</a>(.*?)</div>',
            ask_part,
            re.S,
        )
        asker = re.search(r'rel="face"[^>]*?title="([^"]*)"', ask_part, re.S)
        ask_time = re.search(r'<div class="m_feed_from"[^>]*>\s*<span>([^<]+)</span>', ask_part)
        if not question or not ask_time:
            raise TickHubError(
                "QFSD-9003", f"上证e互动第 {item_id} 条结构改变，无法解析问题或时间"
            )
        answer = answer_time = None
        if answer_part:
            body = re.search(r'<div class="m_feed_txt"[^>]*>(.*?)</div>', answer_part, re.S)
            when = re.search(
                r'<div class="m_feed_from"[^>]*>\s*<span>([^<]+)</span>', answer_part
            )
            if not body or not when:
                raise TickHubError(
                    "QFSD-9003", f"上证e互动第 {item_id} 条有回复块但解析不出回复内容或时间"
                )
            answer = _sse_text(body.group(1))
            answer_time = _sse_required_time(item_id, when.group(1))
        rows.append(
            {
                "id": item_id,
                "code": question.group(2),
                "name": _sse_text(question.group(1)),
                "asker": asker.group(1) if asker else None,
                "question": _sse_text(question.group(3)),
                "question_time": _sse_required_time(item_id, ask_time.group(1)),
                "answer": answer,
                "answer_time": answer_time,
            }
        )
    return rows


@tool("sse_e_interaction", layer="sentiment", ttl="realtime", cache_domain="sentiment")
def sse_e_interaction(
    code: str = "", kind: str = "answered", page: int = 1, page_size: int = 10
) -> dict[str, Any]:
    """SSE e-Interaction: investor questions and Shanghai-listed company replies."""
    if kind not in _SSE_KINDS:
        raise TickHubError("QFSD-9001", "kind 只能是 'answered' 或 'questions'")
    page = int(page or 1)
    page_size = int(page_size or 10)
    if page < 1 or not 1 <= page_size <= 50:
        raise TickHubError("QFSD-9001", "page 从 1 开始，page_size 范围 1–50")

    digits = ""
    if str(code or "").strip():
        digits = normalize_code(code)
        if market_of(code) != "sh":
            raise TickHubError(
                "QFSD-9001",
                f"{code} 不是沪市证券；深市互动问答请用 cninfo_irm，北交所两个平台均无数据",
            )
        text = request(
            "sse",
            "POST",
            _SSE_BASE + "/ajax/userfeeds.do",
            data={
                "typeCode": "company",
                "type": _SSE_KINDS[kind],
                "pageSize": page_size,
                "uid": _sse_company_uid(digits),
                "page": page,
            },
            headers=_SSE_HEADERS,
            expect_json=False,
            encoding="utf-8",
        )
        source_url = _SSE_BASE + "/ajax/userfeeds.do"
    else:
        text = request(
            "sse",
            "GET",
            _SSE_BASE + "/ajax/feeds.do",
            params={"type": _SSE_KINDS[kind], "pageSize": page_size, "lastid": -1, "show": 1, "page": page},
            headers=_SSE_HEADERS,
            expect_json=False,
            encoding="utf-8",
        )
        source_url = _SSE_BASE + "/ajax/feeds.do"

    text = str(text)
    rows = _sse_parse_feed(text)
    # Only the site's explicit "no Q&A" note counts as genuinely empty; silence
    # means the layout changed and must not be reported as an empty result.
    if not rows and not _SSE_EMPTY_NOTE.search(text):
        raise TickHubError(
            "QFSD-9003", "上证e互动返回的页面既没有问答也没有「暂无」提示，结构可能已变"
        )
    if digits and any(row["code"] != digits for row in rows):
        raise TickHubError("QFSD-9003", "上证e互动返回了其他公司的问答，uid 映射可能已变")
    if not rows:
        note = _SSE_NOTE_TEXT.search(text)
        return empty(
            "sse",
            note.group(1).strip() if note else "上证e互动暂无问答",
            source_url=source_url,
            kind=kind,
            page=page,
            page_size=page_size,
            code=digits or None,
        )
    return frame(
        rows,
        "sse",
        source_url=source_url,
        kind=kind,
        page=page,
        page_size=page_size,
        code=digits or None,
    )


@tool("em_hot_concept", layer="sentiment", ttl="realtime", cache_domain="sentiment")
def em_hot_concept(code: str) -> dict[str, Any]:
    """Hot concepts the market currently associates with one stock (Eastmoney)."""
    c = stock_code(code)
    prefix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[market_of(c)]
    payload = request(
        "eastmoney",
        "POST",
        _EM_HOT_CONCEPT,
        data=json.dumps({**_EM_APP, "srcSecurityCode": prefix + c}),
        headers={"Content-Type": "application/json"},
    )
    rows = [
        {
            "concept": item.get("conceptName"),
            "bk": item.get("conceptId"),
            "hit": to_float(item.get("hitCount")),
        }
        for item in (payload or {}).get("data") or []
    ]
    return frame(rows, "eastmoney", source_url=_EM_HOT_CONCEPT, code=c)


__all__ = ["cninfo_irm", "em_hot_concept", "sse_e_interaction"]
