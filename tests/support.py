"""Shared fixtures-as-code for the test suite (plain helpers, not pytest fixtures)."""

from __future__ import annotations

from typing import Any

QT_FIELDS = 49


def qt_line(
    code: str = "600000",
    *,
    name: str = "浦发银行",
    price: float = 10.5,
    prev_close: float = 10.2,
    open_: float = 10.3,
    volume: int = 123456,
    amount: float = 98765432.0,
    market: str = "1",
    timestamp: str = "20261024150000",
    pb: float = 0.72,
    limit_up: float = 11.22,
    limit_down: float = 9.18,
) -> str:
    """Build one Tencent ``v_sh600000="..."`` line with 49 tilde-separated parts."""
    parts: list[str] = [""] * QT_FIELDS
    change = round(price - prev_close, 2)

    def put(index: int, value: Any) -> None:
        parts[index] = str(value)

    put(0, market)
    put(1, name)
    put(2, code)
    put(3, f"{price:.2f}")
    put(4, f"{prev_close:.2f}")
    put(5, f"{open_:.2f}")
    put(6, volume)
    put(7, volume // 2)
    put(8, volume // 2)
    put(9, f"{price - 0.01:.2f}")
    put(10, 1200)
    put(19, f"{price + 0.01:.2f}")
    put(20, 1100)
    put(30, timestamp)
    put(31, f"{change:.2f}")
    put(32, f"{change / prev_close * 100:.2f}")
    put(33, f"{price + 0.2:.2f}")
    put(34, f"{price - 0.3:.2f}")
    put(37, amount)
    put(38, 1.23)
    put(39, 6.54)
    put(43, 2.34)
    put(44, 1.0e6)
    put(45, 2.0e6)
    put(46, f"{pb:.2f}")
    put(47, f"{limit_up:.2f}")
    put(48, f"{limit_down:.2f}")
    symbol = {"1": "sh", "2": "sz"}.get(market, "bj") + code
    return f'v_{symbol}="' + "~".join(parts) + '";'


def kline_payload(code: str = "600000", bars: int = 3) -> dict[str, Any]:
    """Shape returned by web.ifzq.gtimg.cn for a qfq daily request."""
    symbol = "sh" + code
    rows = [
        [f"2026-10-{day:02d}", "10.00", "10.20", "10.40", "9.90", "10000", "1000", "10.10"]
        for day in range(20, 20 + bars)
    ]
    return {"code": 0, "data": {symbol: {"qfqday": rows, "qt": {}}}}


class BoomSession:
    """Stand-in for a requests.Session whose every request fails."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def request(self, *args: Any, **kwargs: Any):
        raise self.exc
