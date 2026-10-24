"""Tongdaxin F10 dossier via the TDX binary protocol (optional ``mootdx`` extra)."""

from __future__ import annotations

import re
from typing import Any

from ..core.config import get_settings
from ..core.errors import TickHubError
from ..core.ticker import market_of
from .base import tool
from .util import ok, stock_code

# mootdx resolves its default server list without a per-server timeout, which
# blocks forever when the first host is unreachable. Pin known-good hosts and
# give the constructor a short timeout instead.
_TDX_SERVERS: tuple[tuple[str, int], ...] = (("119.97.185.59", 7709), ("119.97.185.57", 7709))
_F10_CATEGORY = "最新提示"

_HEADER_RE = re.compile(r"◇(?P<code>\d{6})\s+(?P<name>\S+)\s+更新日期：(?P<date>\d{4}-\d{2}-\d{2})")
_SECTION_RE = re.compile(r"^【(?P<index>\d)\.(?P<title>[^】]+)】\s*(?P<rest>.*)$")
_BLOCK_RE = re.compile(r"^【(?P<title>[^】]+)】\s*(?P<rest>.*)$")
_FEED_RE = re.compile(r"^\s*(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2})│(?P<title>.+?)\s*$")
_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _mootdx():
    settings = get_settings()
    if not settings.enable_mootdx:
        raise TickHubError("QFSD-9002", "mootdx is disabled (set ENABLE_MOOTDX=true to enable F10)")
    try:
        from mootdx.quotes import Quotes
    except Exception as exc:  # noqa: BLE001
        raise TickHubError(
            "QFSD-9002",
            "mootdx is not installed; run pip install 'tickhub-cn[sources-legacy]'",
        ) from exc
    return Quotes


def _f10_body(code: str) -> tuple[str, list[str], str]:
    Quotes = _mootdx()
    last: Exception | None = None
    for server in _TDX_SERVERS:
        client = None
        try:
            client = Quotes.factory(
                market="std",
                server=server,
                timeout=3,
                heartbeat=False,
                auto_retry=False,
            )
            categories = client.F10C(symbol=code) or []
            names = [str(item.get("name") or "") for item in categories if item.get("name")]
            if not names:
                continue
            category = _F10_CATEGORY if _F10_CATEGORY in names else names[0]
            body = client.F10(symbol=code, name=category)
            if body:
                return str(body), names, category
        except Exception as exc:  # noqa: BLE001
            last = exc
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
    raise TickHubError(
        "QFSD-9003",
        f"TDX F10 returned no content for {code}",
        detail={"error": last.__class__.__name__ if last else None},
        retryable=True,
    )


def _cell(value: str) -> Any:
    text = value.strip()
    if text in {"", "---", "--", "-"}:
        return None
    if _NUMBER_RE.match(text):
        return float(text) if "." in text else int(text)
    return text


def _box_table(lines: list[str], start: int) -> tuple[dict[str, Any], int]:
    """Parse one box-drawing block into records plus its single-column footers.

    TDX draws two regions inside the same frame: a multi-column table, then a
    ``├────┴────┤`` separator that collapses it to one column of free text. A
    long label wraps onto a second row *within* its own region, so a one-cell
    row is only a continuation when it still has the header's column count.
    """
    rows: list[list[str]] = []
    index = start
    while index < len(lines):
        line = lines[index].rstrip()
        if line.startswith("└"):
            break
        if line.startswith("│"):
            rows.append([part.strip() for part in line.strip().strip("│").split("│")])
        index += 1

    merged: list[list[str]] = []
    text: list[str] = []
    width = len(rows[0]) if rows else 0
    for cells in rows:
        if width <= 1 or len(cells) != width:
            joined = "".join(cell for cell in cells if cell).strip()
            if joined:
                text.append(joined)
            continue
        if merged and cells[0] and not any(cells[1:]):
            merged[-1][0] = merged[-1][0] + cells[0]
        else:
            merged.append(list(cells))
    if not merged:
        return {"headers": [], "rows": [], "records": [], "text": text}, index
    headers = [cell.lstrip("●").strip() for cell in merged[0]]
    records = []
    for cells in merged[1:]:
        padded = cells + [""] * (len(headers) - len(cells))
        records.append({headers[i]: _cell(padded[i]) for i in range(len(headers))})
    return {
        "headers": headers,
        "rows": [[_cell(cell) for cell in cells] for cells in merged[1:]],
        "records": records,
        "text": text,
    }, index


def _feed_items(lines: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    summary: list[str] = []

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        current["summary"] = "".join(summary).strip()
        items.append(current)
        current = None

    for line in lines:
        match = _FEED_RE.match(line)
        if match:
            flush()
            current = {
                "publish_time": match.group("time"),
                "title": re.sub(r"<[^>]+>", "", match.group("title")).strip(),
                "summary": "",
                "url": "",
            }
            summary = []
            continue
        if current is None:
            continue
        text = line.strip()
        if text.startswith("http"):
            current["url"] = text
        elif text and not text.startswith("─") and "│" not in text:
            summary.append(text)
    flush()
    return items


def _sections(lines: list[str]) -> list[dict[str, Any]]:
    boundaries: list[tuple[int, re.Match[str]]] = []
    for idx, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match:
            boundaries.append((idx, match))
    out: list[dict[str, Any]] = []
    for pos, (start, match) in enumerate(boundaries):
        end = boundaries[pos + 1][0] if pos + 1 < len(boundaries) else len(lines)
        blocks: list[dict[str, Any]] = []
        block: dict[str, Any] | None = None
        text: list[str] = []

        def start_block(title: str, rest: str = "") -> None:
            nonlocal block, text
            if block is not None:
                block["text"] = "\n".join(text).strip()
                blocks.append(block)
            block = {"title": title, "text": "", "lines": [], "tables": []}
            text = [rest] if rest.strip() else []
            if rest.strip():
                block["lines"].append(rest)

        start_block("", match.group("rest"))
        index = start + 1
        while index < end:
            line = lines[index].rstrip()
            sub = _BLOCK_RE.match(line)
            if sub:
                start_block(sub.group("title"), sub.group("rest"))
            elif line.startswith("┌"):
                table, index = _box_table(lines, index)
                block["tables"].append(table)  # type: ignore[index]
                # Footer text of the box is real content (latest announcements,
                # year-on-year figures); keep it readable next to the table.
                block["lines"].extend(table.get("text") or [])  # type: ignore[index]
                text.extend(table.get("text") or [])
            elif line.startswith("〖"):
                break
            elif line.strip():
                text.append(line.strip())
                block["lines"].append(line)  # type: ignore[index]
            index += 1
        if block is not None:
            block["text"] = "\n".join(text).strip()
            blocks.append(block)
        out.append({"index": int(match.group("index")), "title": match.group("title"), "blocks": blocks})
    return out


@tool(layer="financials", ttl="semi_static", cache_domain="f10")
def stock_f10_tips(code: str) -> dict[str, Any]:
    """TDX F10 latest-tips dossier: indicators, announcements, news, block trades and margin (mootdx)."""
    c = stock_code(code)
    if market_of(c) == "bj":
        raise TickHubError("QFSD-9002", "TDX F10 covers Shanghai/Shenzhen only; Beijing codes are not served")
    body, categories, category = _f10_body(c)
    lines = body.splitlines()
    header = _HEADER_RE.search(lines[0]) if lines else None
    sections = _sections(lines)
    by_index = {section["index"]: section for section in sections}

    def first_block_text(section_index: int, title: str) -> str:
        for block in by_index.get(section_index, {}).get("blocks", []):
            if block["title"] == title:
                return str(block["text"] or "")
        return ""

    announcements: list[dict[str, Any]] = []
    news: list[dict[str, Any]] = []
    for section in sections:
        if section["index"] == 3:
            for block in section["blocks"]:
                announcements.extend(_feed_items(block.get("lines") or []))
        elif section["index"] == 4:
            for block in section["blocks"]:
                news.extend(_feed_items(block.get("lines") or []))

    business = ""
    for section in sections:
        for block in section["blocks"]:
            if block["title"] == "主营业务":
                business = str(block["text"] or "")

    data = {
        "code": c,
        "name": header.group("name") if header else None,
        "updated": header.group("date") if header else None,
        "category": category,
        "categories": categories,
        "business": business or None,
        "announcements": announcements,
        "news": news,
        "sections": sections,
    }
    return ok(
        data,
        "mootdx",
        extra={
            "count": len(sections),
            "upstream": "通达信 F10 (TDX binary protocol)",
            "server": f"{_TDX_SERVERS[0][0]}:{_TDX_SERVERS[0][1]}",
        },
    )


__all__ = ["stock_f10_tips"]
