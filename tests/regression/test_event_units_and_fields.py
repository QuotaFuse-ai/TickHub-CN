"""QUOT-249 / QUOT-266: A-share event amounts are 元 and volumes are 股, scaled
exactly once, with the new fields.

Covers the three ways this contract breaks in practice: a scale applied twice
(datasource plus event), a scale applied nowhere, and a missing vendor line
filled in from a neighbouring column.
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tickhub_cn.crawler import event_publisher, store
from tickhub_cn.crawler.store import _financial_event, _kline_event, _northbound_event, _quote_event

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


# =============================================================== QUOT-266 below
#
# The same 1e4/1e2 class of defect, on the two columns QUOT-258 left standing:
# northbound money published in 万元 next to a quote market cap in 元, and K-line
# volume in 手 next to a warehouse column in 股. A unit error is invisible in a
# single field -- the number looks like a number -- so each one is pinned from
# three sides: the arithmetic, the count of scale sites in the source, and a
# cross-check against a figure the vendor publishes independently.

_STORE_SRC = (SRC / "crawler" / "store.py").read_text(encoding="utf-8")
_FUNDS_SRC = (SRC / "datasources" / "funds.py").read_text(encoding="utf-8")
_QUOTES_SRC = (SRC / "datasources" / "quotes.py").read_text(encoding="utf-8")


def _code_lines(source: str) -> str:
    """Source with comments and docstrings blanked out, lines kept in place.

    A scale factor is a fact about the code; a docstring that explains the lot
    size must not read like a second conversion site. Blanking instead of
    deleting keeps every node offset valid for the extraction below.
    """
    lines = source.splitlines()
    docstring: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            head = node.body[0] if node.body else None
            if (
                isinstance(head, ast.Expr)
                and isinstance(head.value, ast.Constant)
                and isinstance(head.value.value, str)
            ):
                docstring.update(range(head.lineno, (head.end_lineno or head.lineno) + 1))
    return "\n".join(
        "" if number in docstring or text.lstrip().startswith("#") else text
        for number, text in enumerate(lines, start=1)
    )


def _named(source: str, names: set[str]) -> str:
    """Code of the named top-level constants and functions -- one unit path."""
    code = _code_lines(source)
    kept: list[str] = []
    for node in ast.parse(code).body:
        targets: set[str] = set()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            targets = {node.name}
        elif isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = {node.target.id}
        if targets & names:
            segment = ast.get_source_segment(code, node)
            kept.append(segment if segment is not None else "")
    return "\n".join(kept)


def _sites(source: str, factor: str) -> int:
    """Times a bare factor appears, not as a slice of a longer number."""
    return len(re.findall(rf"(?<![\d.]){factor}(?![\d])", source))


# 工商银行 2026-10-30, as the vendor sent it: 4315746 手 at a close of 8.75 元,
# and Tencent reports 3760466142 元 traded for that same bar. Three numbers from
# two different columns of one feed, so they can only line up if volume is 股.
_KLINE_ROW: dict[str, Any] = {
    "date": "2026-10-30",
    "open": 8.61,
    "high": 8.79,
    "low": 8.6,
    "close": 8.75,
    "volume": 4315746,
}
_TENCENT_TURNOVER_YUAN = Decimal("3760466142")


def _kline_bar(**overrides: Any) -> dict[str, Any]:
    row = dict(_KLINE_ROW)
    row.update(overrides)
    return _kline_event("601398", row, period="day", adjust="qfq")["content"]["structured_fields"]


def _northbound_fields(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": "沪股通",
        "net_inflow": "123456.78",  # 万元, the unit funds.py declares in its meta
        "deal_amount_wan": "15625100.0",  # 万元
        "deal_num": 8382898,
        "quota_text": "额度充足",
    }
    row.update(overrides)
    event = _northbound_event("2026-10-30", "hk2sh", row)
    return event["content"]["structured_fields"]


# --------------------------------------------------------- northbound: 万元→元


def test_northbound_net_buy_amount_is_yuan_not_wan() -> None:
    fields = _northbound_fields()
    assert fields["net_buy_amount"] == "1234567800.0000"
    assert Decimal(fields["net_buy_amount"]) == Decimal("123456.78") * Decimal("10000")


def test_northbound_net_selling_keeps_its_sign() -> None:
    # A net sell is a real state, not a missing value, and 12.3亿 of it is an
    # ordinary day -- which is exactly why the 万元 reading (1.2万亿) cannot be
    # the contract's: it would exceed the whole market's daily turnover.
    assert _northbound_fields(net_inflow=-500000.0)["net_buy_amount"] == "-5000000000.0000"
    assert Decimal("1e8") < Decimal(_northbound_fields()["net_buy_amount"]) < Decimal("1e10")


@pytest.mark.parametrize("blank", [None, "", "-"])
def test_northbound_amount_stays_absent_instead_of_becoming_zero(blank: Any) -> None:
    assert "net_buy_amount" not in _northbound_fields(net_inflow=blank)
    assert "deal_amount" not in _northbound_fields(deal_amount_wan=blank)


def test_northbound_turnover_is_renamed_along_with_the_scale() -> None:
    fields = _northbound_fields()
    assert fields["deal_amount"] == "156251000000.0000"
    # The name carried the unit; keeping it next to a 元 value is how the old
    # scale survives a rename that stopped halfway.
    assert "deal_amount_wan" not in fields


def test_wan_scale_exists_once_in_the_module_and_nowhere_upstream() -> None:
    path = _named(_STORE_SRC, {"_WAN", "_yuan_from_wan", "_northbound_event"})
    assert _sites(path, "10000") == 1
    assert _sites(_code_lines(_STORE_SRC), "10000") == 1
    # funds.py does multiply -- 百万元 history rows up to its own 万元 tool
    # contract -- but it must stop there, and the only 1e4 in that file is the
    # ETF quote helper, which no northbound row ever passes through.
    assert _sites(_named(_FUNDS_SRC, {"_northbound_history", "northbound_flow"}), "10000") == 0
    assert _sites(_named(_FUNDS_SRC, {"_wan_to_yuan"}), "10000") == 1


# ------------------------------------------------------------- kline: 手→股


def test_kline_volume_is_shares_not_lots() -> None:
    assert _kline_bar()["volume"] == "431574600"
    assert Decimal(_kline_bar()["volume"]) == Decimal("4315746") * Decimal("100")


@pytest.mark.parametrize(
    ("volume", "expected"),
    [(4315746, "431574600"), ("4315746.0", "431574600"), (0, "0"), ("-", "-"), ("n/a", "n/a")],
)
def test_lots_conversion_keeps_the_old_fallback_for_unreadable_text(
    volume: Any, expected: str | None
) -> None:
    assert _kline_bar(volume=volume)["volume"] == expected


@pytest.mark.parametrize("missing", [None, ""])
def test_absent_volume_is_dropped_not_zero_filled(missing: Any) -> None:
    assert "volume" not in _kline_bar(volume=missing)


def test_lots_scale_exists_once_in_the_module_and_not_in_the_tool() -> None:
    path = _named(_STORE_SRC, {"_LOTS", "_shares_from_lots", "_kline_fields", "_kline_event"})
    assert _sites(path, "100") == 1
    assert _sites(_code_lines(_STORE_SRC), "100") == 1
    # tencent_kline mirrors the vendor and its callers read 手, so the tool layer
    # must not have quietly converted already.
    assert _sites(_named(_QUOTES_SRC, {"tencent_kline"}), "100") == 0


def test_volume_and_close_reconcile_with_the_vendors_turnover() -> None:
    fields = _kline_bar()
    implied = Decimal(fields["volume"]) * Decimal(fields["close"])
    ratio = implied / _TENCENT_TURNOVER_YUAN
    # Close is not VWAP, so this band is about the order of magnitude, not the
    # last digits: 0.4% apart on the real record.
    assert Decimal("0.95") < ratio < Decimal("1.05")


def test_a_lots_volume_is_a_hundred_times_too_small_to_pass_that_band() -> None:
    # The counter-example the band exists for: the unscaled bar is off by exactly
    # the lot size, and a test that only eyeballed the number would have passed.
    ratio = (Decimal("4315746") * Decimal("8.75")) / _TENCENT_TURNOVER_YUAN
    assert Decimal("0.005") < ratio < Decimal("0.05")


# ---------------------------------------------------------------- key generations


def test_kline_key_carries_the_generation_so_the_fix_lands() -> None:
    event = _kline_event("601398", _KLINE_ROW, period="day", adjust="qfq")
    assert event["biz_key"] == "2026-10-30|day|qfq|g2"
    legacy = event_publisher.event_id("tencent", "kline", "601398", "2026-10-30|day|qfq")
    assert event["event_id"] != legacy
    # FinanceCoffer versions K-line rows, so the new key becomes a new version
    # instead of a replay of the 手 bar already in the warehouse.
    again = [_kline_event("601398", _KLINE_ROW, period="day", adjust="qfq") for _ in range(3)]
    assert {each["event_id"] for each in again} == {event["event_id"]}


def test_northbound_key_stays_untagged() -> None:
    # Deliberately the one event type without a generation suffix: its DWD table
    # is insert_once on (trade_date, channel), so a new key would put two rows of
    # two different magnitudes into ODS for one day and the DWD would still keep
    # only the first. New dates get the new unit; old dates are a manual repair.
    event = _northbound_event("2026-10-30", "hk2sh", {"name": "沪股通"})
    assert event["biz_key"] == "2026-10-30|hk2sh"
    assert not event["biz_key"].endswith(f"|{store._GENERATION}")

