"""Shenwan industry classification history from the official SW Research workbook."""

from __future__ import annotations

import io
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from ..core.errors import TickHubError
from ..core.http import fetch
from .base import tool
from .util import empty, ok, require_date, stock_code

# The host ships a leaf certificate without its intermediate, so the CA bundle
# is certifi plus the vendored GeoTrust G2 cross-signed root. Verification is
# never disabled.
_SW_XLS = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
_CA_EXTRA = "geotrust-g2-tls-cn-rsa4096-sha256-2022-ca1.pem"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/vnd.ms-excel,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.swsresearch.com/swindex/",
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_CA_PATH: str | None = None
_ROWS: tuple[float, list[dict[str, Any]]] | None = None
_ROWS_TTL = 6 * 3600


def _ca_bundle() -> bool | str:
    """certifi plus the intermediate the Shenwan host omits, merged once."""
    global _CA_PATH
    if _CA_PATH:
        return _CA_PATH
    extra = Path(__file__).resolve().parent.parent / "data" / _CA_EXTRA
    if not extra.exists():
        return True
    try:
        import certifi

        text = Path(certifi.where()).read_text(encoding="utf-8") + "\n" + extra.read_text(encoding="utf-8")
        path = Path(tempfile.gettempdir()) / f"tickhub-ca-{abs(hash(text)) & 0xFFFFFFFF:08x}.pem"
        if not path.exists():
            path.write_text(text, encoding="utf-8")
        _CA_PATH = str(path)
    except Exception:  # noqa: BLE001
        return True
    return _CA_PATH


def _iso(value: Any) -> str:
    text = str(value)[:10] if value is not None else ""
    return text if _DATE_RE.match(text) else ""


def _download() -> bytes:
    last: Exception | None = None
    for attempt in range(3):
        try:
            resp = fetch(
                "swsresearch",
                "GET",
                _SW_XLS,
                headers=_HEADERS,
                timeout=30,
                retries=1,
                verify=_ca_bundle(),
            )
            if len(resp.content) > 1024:
                return resp.content
            last = ValueError(f"empty workbook (HTTP {resp.status_code}, {len(resp.content)} bytes)")
        except TickHubError as exc:
            last = exc
        if attempt < 2:
            time.sleep(1.0 + attempt)
    raise TickHubError(
        "QFSD-9003",
        f"申万行业分类表下载失败: {last}",
        detail={"url": _SW_XLS},
        retryable=True,
    )


def _parse(content: bytes) -> list[dict[str, Any]]:
    try:
        import pandas as pd
    except Exception as exc:  # noqa: BLE001
        raise TickHubError(
            "QFSD-9002",
            "pandas/xlrd required for the Shenwan workbook; run pip install 'tickhub-cn[sources]'",
        ) from exc
    try:
        frame = pd.read_excel(io.BytesIO(content), dtype={"股票代码": "str", "行业代码": "str"})
    except Exception as exc:  # noqa: BLE001
        raise TickHubError("QFSD-9003", f"申万行业分类表解析失败: {exc}", retryable=True) from exc

    rows: list[dict[str, Any]] = []
    for record in frame.to_dict("records"):
        code = str(record.get("股票代码") or "").strip()
        industry = str(record.get("行业代码") or "").strip()
        start = _iso(record.get("计入日期"))
        if not code.isdigit() or not start or not industry.isdigit():
            continue
        rows.append(
            {
                "code": code.zfill(6),
                "start_date": start,
                "industry_code": industry.zfill(6),
                "update_time": _iso(record.get("更新日期")) or None,
            }
        )
    rows.sort(key=lambda row: (row["code"], row["start_date"]))
    return rows


def _load_rows() -> list[dict[str, Any]]:
    global _ROWS
    now = time.time()
    if _ROWS is not None and now - _ROWS[0] < _ROWS_TTL:
        return _ROWS[1]
    rows = _parse(_download())
    _ROWS = (now, rows)
    return rows


@tool(layer="index", ttl="static", cache_domain="sw_industry")
def sw_industry_history(code: str, as_of: str = "", limit: int = 0) -> dict[str, Any]:
    """Shenwan industry classification history for one stock (SW Research workbook)."""
    c = stock_code(code)
    as_of_date = require_date(as_of or None, "")
    rows = [row for row in _load_rows() if row["code"] == c]
    if not rows:
        return empty("swsresearch", f"{c} 不在申万行业分类表中（北交所或未纳入分类的标的）")

    history: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["l1_code"] = row["industry_code"][:2] + "0000"
        item["l2_code"] = row["industry_code"][:4] + "00"
        item["l3_code"] = row["industry_code"]
        if history and history[-1]["start_date"] == item["start_date"] and history[-1]["industry_code"] == item["industry_code"]:
            continue
        history.append(item)

    current = history[-1]
    if as_of_date:
        eligible = [item for item in history if item["start_date"] <= as_of_date]
        if not eligible:
            return ok(
                {"code": c, "as_of": as_of_date, "current": None, "history": [], "total": len(history)},
                "swsresearch",
                extra={"count": 0, "note": f"as_of 早于首个分类计入日期 {history[0]['start_date']}"},
            )
        current = eligible[-1]

    for item in history:
        item["current"] = item is current

    shown = history
    if limit and limit > 0:
        shown = history[-min(int(limit), len(history)) :]
    return ok(
        {
            "code": c,
            "as_of": as_of_date or None,
            "current": {key: value for key, value in current.items() if key != "current"},
            "history": shown,
            "total": len(history),
            "first_seen": history[0]["start_date"],
            "last_changed": current["start_date"],
        },
        "swsresearch",
        extra={
            "count": len(shown),
            "upstream": "申万宏源研究 SwClass2021/StockClassifyUse_stock.xls",
            "note": "分类表只发布行业代码（前2位一级、中2位二级、后2位三级），不含名称",
        },
    )


__all__ = ["sw_industry_history"]
