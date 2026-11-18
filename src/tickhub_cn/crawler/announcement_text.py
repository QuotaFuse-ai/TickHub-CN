"""公告正文抽取：PDF → 纯文本（纯本地，不调模型）。

cninfo / 深交所的公告列表只给标题和 PDF 链接，正文必须自己拉。这里用 ``pypdf``
（TickHub 依赖里已有）抽文本，再交给 ``cleaner.strip_html`` 去页面噪声。

任何失败（超时、403、扫描版 PDF 抽不出字）都返回 ``None``，调用方退回标题——
正文缺失不该让一次采集失败，也不该编造内容。
"""

from __future__ import annotations

import io
import time
import urllib.request
from typing import Any

from ..logging.setup import get_logger
from .cleaner import strip_html

MAX_PAGES = 40
MAX_CHARS = 12000
TIMEOUT = 15.0
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/pdf,application/octet-stream,*/*",
}


def _log() -> Any:
    return get_logger("crawler")


def fetch_pdf_bytes(url: str, *, timeout: float = TIMEOUT, attempts: int = 2) -> bytes | None:
    """下载 PDF；cninfo 偶发慢响应，给一次重试再放弃。"""
    if not url:
        return None
    last: str = ""
    for attempt in range(max(1, attempts)):
        request = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - 网络侧的一切失败都按「没取到」处理
            last = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < attempts:
                time.sleep(1.0)
    _log().warning(
        "announcement pdf download failed",
        extra={
            "event": "pdf_download_failed",
            "error_code": "QFSD-9003",
            "detail": {"url": url[:120], "error": last[:160]},
        },
    )
    return None


def pdf_text(data: bytes, *, max_pages: int = MAX_PAGES, max_chars: int = MAX_CHARS) -> str | None:
    """从 PDF 字节抽正文；扫描版（无文字层）返回 None，不拿 OCR 猜。"""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - 依赖缺失时退回标题
        _log().warning(
            "pypdf missing; announcement body skipped",
            extra={"event": "pdf_reader_missing", "error_code": "QFSD-9001"},
        )
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - 损坏/加密的 PDF
        _log().warning(
            "announcement pdf unreadable",
            extra={
                "event": "pdf_unreadable",
                "detail": {"error": f"{type(exc).__name__}: {exc}"[:160]},
            },
        )
        return None

    pages: list[str] = []
    for index, page in enumerate(reader.pages):
        if index >= max_pages:
            break
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - 单页出错不影响其余页
            continue
    text = strip_html("\n".join(pages))
    if not text:
        return None
    return text[:max_chars]


def fetch_announcement_text(url: str, **kwargs: Any) -> str | None:
    data = fetch_pdf_bytes(url, timeout=float(kwargs.get("timeout", TIMEOUT)))
    if not data:
        return None
    return pdf_text(
        data,
        max_pages=int(kwargs.get("max_pages", MAX_PAGES)),
        max_chars=int(kwargs.get("max_chars", MAX_CHARS)),
    )


def attach_texts(
    rows: list[dict[str, Any]],
    *,
    max_workers: int = 3,
    max_chars: int = MAX_CHARS,
) -> int:
    """给每条公告补 ``row["_text"]``（正文）；返回成功条数。

    并发固定在 3 以内：公告站点对外链有频控，拉太猛会被 403，反而一条都拿不到。
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(row: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        url = str(row.get("url") or row.get("adjunctUrl") or row.get("pdf") or "")
        return row, fetch_announcement_text(url, max_chars=max_chars)

    filled = 0
    with ThreadPoolExecutor(max_workers=max(1, min(3, max_workers))) as pool:
        for row, text in pool.map(one, rows):
            if text:
                row["_text"] = text
                filled += 1
    return filled
