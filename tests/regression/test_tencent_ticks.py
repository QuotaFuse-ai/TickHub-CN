"""Tick page walk and the post-close turnover completeness gate."""

from __future__ import annotations

import pytest

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import quotes
from tests.support import qt_line


def _page(start: int, size: int, *, side: str = "B", volume: int = 10, amount: float = 9500.0) -> str:
    chunks = "|".join(
        f"{start + i}/09:30:{i % 60:02d}/9.50/0.01/{volume}/{amount:.1f}/{side}" for i in range(size)
    )
    return f'v_detail_data_sh600000=[{start // 70},"{chunks}"];'


def _dispatcher(pages: dict[int, str], *, quote: str | Exception = ""):
    def fake(source: str, method: str, url: str, *, params: dict | None = None, **kwargs):
        if url.startswith(quotes._QT):
            if isinstance(quote, Exception):
                raise quote
            return quote
        return pages.get(int((params or {}).get("p", 0)), "")

    return fake


def test_the_first_page_is_page_zero_and_neutral_is_its_own_side(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []

    def fake(source: str, method: str, url: str, *, params: dict | None = None, **kwargs):
        seen.append(dict(params or {}))
        return _page(0, 3, side="M")

    monkeypatch.setattr(quotes, "request", fake)
    payload = quotes.tencent_ticks("600000")

    assert seen[0]["p"] == "0"
    assert payload["data"]["full"] is False
    assert [row["side"] for row in payload["data"]["ticks"]] == ["neutral"] * 3


def test_a_full_walk_stops_on_the_short_page_and_reconciles(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {0: _page(0, 70), 1: _page(70, 70), 2: _page(140, 3, side="S")}
    monkeypatch.setattr(
        quotes,
        "request",
        _dispatcher(pages, quote=qt_line(volume=1430, amount=135.85, timestamp="20261023150003")),
    )

    payload = quotes.tencent_ticks("600000", full=True)
    meta = payload["meta"]

    assert meta["pages"] == 3
    assert meta["count"] == 143
    assert meta["truncated"] is False
    assert meta["sides"] == {"buy": 140, "sell": 3}
    assert meta["completeness"]["complete"] is True
    assert meta["completeness"]["session_closed"] is True
    assert meta["completeness"]["amount_ratio"] == 1.0
    assert meta["completeness"]["volume_ratio"] == 1.0
    assert "degraded" not in meta


def test_a_short_tick_sum_is_flagged_as_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {0: _page(0, 70), 1: _page(70, 5)}
    monkeypatch.setattr(
        quotes,
        "request",
        _dispatcher(pages, quote=qt_line(volume=7500, amount=712.5, timestamp="20261023150003")),
    )

    meta = quotes.tencent_ticks("600000", full=True)["meta"]

    assert meta["completeness"]["complete"] is False
    assert meta["degraded"] == "ticks_incomplete"
    assert "偏差" in meta["note"]


def test_an_open_session_gets_no_completeness_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {0: _page(0, 70), 1: _page(70, 70)}
    monkeypatch.setattr(
        quotes,
        "request",
        _dispatcher(pages, quote=qt_line(volume=1400, amount=133.0, timestamp="20261023143000")),
    )

    meta = quotes.tencent_ticks("600000", full=True)["meta"]

    assert meta["completeness"]["session_closed"] is False
    assert meta["completeness"]["complete"] is None
    assert "15:00" in meta["note"]
    assert "degraded" not in meta


def test_a_quote_failure_leaves_the_ticks_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {0: _page(0, 70), 1: _page(70, 2)}
    monkeypatch.setattr(
        quotes,
        "request",
        _dispatcher(pages, quote=TickHubError("QFSD-9003", "quote blocked")),
    )

    payload = quotes.tencent_ticks("600000", full=True)

    assert payload["meta"]["count"] == 72
    assert payload["meta"]["completeness"]["checked"] is False
    assert "quote blocked" in payload["meta"]["completeness"]["reason"]
    assert "degraded" not in payload["meta"]


def test_an_empty_first_page_returns_no_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quotes, "request", _dispatcher({}, quote=qt_line()))

    payload = quotes.tencent_ticks("600000", full=True)

    assert payload["data"]["ticks"] == []
    assert payload["meta"]["pages"] == 0
    assert payload["meta"]["completeness"]["checked"] is True
