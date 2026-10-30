"""QUOT-249: A-share event amounts are 元, scaled exactly once, with the two new fields.

Covers the three ways this contract breaks in practice: a scale applied twice
(datasource plus event), a scale applied nowhere, and a missing vendor line
filled in from a neighbouring column.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tickhub_cn.crawler import event_publisher, store
from tickhub_cn.crawler.store import _financial_event, _quote_event

SRC = Path(store.__file__).parent.parent

_VENDOR_YI = "31185.55"  # 工商银行 total market cap, 亿元, as Tencent sends it


def _quote_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "price": 8.75,
        "pe": 8.12,
        "pb": 0.78,
        "total_market_cap": float(_VENDOR_YI),
        "float_market_cap": 23591.07,
    }
    row.update(overrides)
    return row


def _quote_fields(**overrides: Any) -> dict[str, Any]:
    event = _quote_event("601398", _quote_row(**overrides), trade_date="2026-10-30", stamp=None)
    return event["content"]["structured_fields"]


def _income_fields(**row_overrides: Any) -> dict[str, Any]:
    row = {
        "report_date": "2026-12-31",
        "revenue": 695947000000.0,
        "net_profit": 288278000000.0,
        "parent_net_profit": 285640000000.0,
        "deducted_net_profit": 284926000000.0,
    }
    row.update({key: value for key, value in row_overrides.items()})
    event = _financial_event("income", row, code="601398", report_date="2026-12-31", check=None)
    return event["content"]["structured_fields"]


# ----------------------------------------------------------------- market cap
#
# The vendor number is 亿元 and the contract is 元, so exactly one 1e8 has to
# appear between the two. These tests pin that from both ends: the arithmetic,
# and the absence of a second multiplication anywhere on the event path.


def test_quote_event_publishes_market_cap_in_yuan() -> None:
    fields = _quote_fields()
    assert fields["total_market_cap"] == "3118555000000.0000"
    assert Decimal(fields["total_market_cap"]) == Decimal(_VENDOR_YI) * Decimal("1e8")


def test_market_cap_is_scaled_once_not_twice() -> None:
    fields = _quote_fields()
    scaled = Decimal(fields["total_market_cap"])
    assert scaled / Decimal(_VENDOR_YI) == Decimal("100000000")
    # A second conversion anywhere in the pipeline would put the ratio at 1e16.
    assert scaled / Decimal(_VENDOR_YI) < Decimal("1e9")


def test_event_layer_holds_the_only_scale_site() -> None:
    pattern = re.compile(r"1e8|100000000|scaleb\(")

    def code_lines(path: Path) -> str:
        lines = path.read_text(encoding="utf-8").splitlines()
        return "\n".join(line for line in lines if not line.lstrip().startswith("#"))

    scanned = [SRC / "crawler" / "store.py", SRC / "datasources" / "quotes.py"]
    hits = {path.name: len(pattern.findall(code_lines(path))) for path in scanned}
    assert hits == {"store.py": 1, "quotes.py": 0}


def test_market_cap_keeps_four_decimals_without_float_drift() -> None:
    # 12345678.9012345 亿元 is 1.23e15 元: past the 17 significant digits a double
    # can carry, so a float pipeline would invent tail digits the vendor never sent.
    fields = _quote_fields(total_market_cap="12345678.9012345")
    assert fields["total_market_cap"] == "1234567890123450.0000"


def test_absent_market_cap_is_dropped_from_the_event() -> None:
    for value in (None, "", "-"):
        assert "total_market_cap" not in _quote_fields(total_market_cap=value)


# ---------------------------------------------------------------------------- pb


@pytest.mark.parametrize(
    ("pb", "expected"),
    [(0.78, "0.7800"), (6.42, "6.4200"), (0.0, None), (-1.2, None), (None, None), ("", None)],
)
def test_pb_mrq_passes_the_vendor_ratio_only_when_positive(pb: Any, expected: str | None) -> None:
    fields = _quote_fields(pb=pb)
    assert fields.get("pb_mrq") == expected
    assert ("pb" in fields) == (pb not in (None, ""))


def test_pb_and_pb_mrq_carry_the_same_number() -> None:
    fields = _quote_fields()
    assert fields["pb"] == fields["pb_mrq"] == "0.7800"


# ----------------------------------------------------------- deducted profit


def test_financial_event_carries_deducted_profit_in_yuan() -> None:
    fields = _income_fields()
    assert fields["amount_net_profit_deducted"] == "284926000000.0000"
    assert fields["amount_net_profit"] == "288278000000.0000"


@pytest.mark.parametrize("missing", ["deducted_net_profit", "parent_net_profit", "net_profit"])
def test_deducted_profit_is_never_borrowed_from_another_column(missing: str) -> None:
    fields = _income_fields(**{missing: None})
    if missing == "deducted_net_profit":
        assert "amount_net_profit_deducted" not in fields
    else:
        assert fields["amount_net_profit_deducted"] == "284926000000.0000"


def test_zero_deducted_profit_is_still_published() -> None:
    fields = _income_fields(deducted_net_profit=0.0)
    assert fields["amount_net_profit_deducted"] == "0.0000"


# ------------------------------------------------------------ cross-field rule
#
# total_market_cap (元) / annual net profit (元) is a price/earnings ratio. It
# can only land near the vendor pe_ttm if BOTH amounts are in the basic unit, so
# this is the assertion that fails when only one side of the cutover is done.


def test_market_cap_and_net_profit_agree_with_the_vendor_pe() -> None:
    fields = _quote_fields()
    profit = Decimal(_income_fields()["amount_net_profit"])
    implied_pe = Decimal(fields["total_market_cap"]) / profit
    vendor_pe = Decimal(fields["pe_ttm"])
    assert Decimal("0.5") < implied_pe / vendor_pe < Decimal("2.0")
    assert 5 < implied_pe < 30


def test_a_single_sided_cutover_breaks_the_pe_consistency() -> None:
    # The old generation: cap still in 亿元 next to a profit in 元.
    unscaled = Decimal(_VENDOR_YI)
    profit = Decimal(_income_fields()["amount_net_profit"])
    assert unscaled / profit < Decimal("1e-6")


# ------------------------------------------------------------- re-publish path
#
# FinanceCoffer's DWD layer is append-only (a BEFORE UPDATE trigger rejects
# business edits) and skips events whose event_id it has seen, and event_id
# hashes provider|data_type|symbol|biz_key -- never the payload. So a corrected
# amount re-published under the key the 亿元 row already used would be dropped
# as a replay: the fix would live in the code and never reach the warehouse.
# The business keys carry the field generation instead, which makes the re-run a
# new version of the same fact.


def _quote_event_id(biz_key: str) -> str:
    return event_publisher.event_id("tencent", "quote", "601398", biz_key)


def _financial_event_id(biz_key: str) -> str:
    return event_publisher.event_id("eastmoney", "financial", "601398", biz_key)


def test_fixed_quote_does_not_hash_to_the_stored_event_id() -> None:
    event = _quote_event("601398", _quote_row(), trade_date="2026-10-30", stamp=None)
    legacy_day = "2026-10-30|2026-10-30"
    assert event["biz_key"] != legacy_day
    assert event["event_id"] != _quote_event_id(legacy_day)


def test_fixed_financial_does_not_hash_to_the_stored_event_id() -> None:
    row = {
        "revenue": 695947000000.0,
        "net_profit": 288278000000.0,
        "deducted_net_profit": 284926000000.0,
    }
    event = _financial_event("income", row, code="601398", report_date="2026-12-31", check=None)
    assert event["biz_key"] != "2026-12-31|income"
    assert event["event_id"] != _financial_event_id("2026-12-31|income")


def test_generation_tag_is_a_constant_not_a_timestamp() -> None:
    # A tag that varies per run would re-publish every archived day on every
    # re-run; the key must be stable so idempotence still holds within a run.
    events = [
        _quote_event("601398", _quote_row(), trade_date="2026-10-30", stamp=None)
        for _ in range(3)
    ]
    assert {event["event_id"] for event in events} == {events[0]["event_id"]}
    assert store._GENERATION == "g2"
    assert events[0]["biz_key"].endswith("|g2")
