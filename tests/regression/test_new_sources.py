"""Regressions for the nine tools added while closing the reference-repo gaps.

Every case pins one upstream payload shape plus the transformation on top of
it, so endpoint drift or a wrong unit conversion fails offline.
"""

from __future__ import annotations

import datetime as _dt
import types

import pytest

import tickhub_cn.datasources  # noqa: F401  (registers aliases)
from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import events, funds, market
from tickhub_cn.datasources.base import ALIASES, resolve


# --------------------------------------------------------------------------- #
# Market pools
# --------------------------------------------------------------------------- #


def test_yesterday_limit_up_pool_scales_price_and_time(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "data": {
            "pool": [
                {
                    "c": "600000",
                    "n": "浦发银行",
                    "m": 1,
                    "p": 11230,
                    "zdp": 10.02,
                    "hs": 3.4,
                    "yfbt": 92500,
                    "ylbc": 2,
                    "zttj": {"days": 3, "ct": 4},
                },
                {"c": "830799", "n": "艾融软件", "m": 0, "p": 20100, "zttj": {}},
            ]
        }
    }
    monkeypatch.setattr(market, "em_get", lambda *a, **k: payload)

    out = market.yesterday_limit_up_pool("2026-10-23")

    first = out["data"][0]
    assert first["price"] == 11.23
    assert first["y_first_seal_time"] == "09:25:00"
    assert first["limit_up_days"] == 3
    # m=0 is used by both SZ and BJ, so the code range must win.
    assert out["data"][1]["market"] == "bj"
    assert out["meta"]["date"] == "20261023"


def test_monitor_pool_keeps_only_active_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = _dt.datetime(2026, 10, 23, 15, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    monkeypatch.setattr(market, "now_shanghai", lambda: fixed)
    monkeypatch.setattr(
        market,
        "request",
        lambda *a, **k: [
            {"STKCODE": "002731", "STKNAME": "老监控", "MARKET": "0", "VALIDATESTARTDATE": "2026-09-01", "VALIDATEENDDATE": "2026-10-01"},
            {"STKCODE": "600000", "STKNAME": "新监控", "MARKET": "1", "VALIDATESTARTDATE": "2026-10-22", "VALIDATEENDDATE": "2026-11-05"},
            {"STKCODE": "920001", "STKNAME": "未知市场", "MARKET": "H", "VALIDATESTARTDATE": "2026-10-22", "VALIDATEENDDATE": "2026-11-05"},
        ],
    )

    out = market.monitor_pool(only_active=True)

    codes = [row["code"] for row in out["data"]]
    assert codes == ["600000", "920001"]
    # Unknown market letters are surfaced, never guessed.
    assert out["data"][1]["market"] == "?H"


def test_anomaly_list_maps_rule_codes_and_history_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        market,
        "_anomaly_get",
        lambda *a, **k: {
            "result": 0,
            "date": "20261023",
            "pages": 2,
            "data": [
                {"c": "300001", "n": "A", "m": 0, "s": 6, "e": 4, "a": 12.3, "x": 101.2, "d": 10, "o": 1},
                {"c": "830799", "n": "B", "m": 0, "s": 8, "e": 8, "a": -5.0, "x": -30.0, "d": 10, "o": 2},
            ],
        },
    )

    out = market.price_anomaly()
    rows = out["data"]

    # Board 6 (ChiNext) rule 4 is the +150% variant, i.e. 40.
    assert rows[0]["rule_code"] == 40
    assert rows[0]["market"] == "SZ"
    assert rows[0]["is_today"] is True
    assert rows[1]["market"] == "BJ"
    assert rows[1]["is_today"] is False
    assert out["meta"]["pages"] == 2


def test_anomaly_rejects_a_vendor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        market,
        "em_get",
        lambda *a, **k: {"result": 1, "msg": "unknow team", "data": []},
    )

    with pytest.raises(TickHubError) as exc:
        market.price_anomaly_count()

    assert exc.value.code == "QFSD-9003"


def test_ths_limit_up_pool_formats_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        market,
        "request",
        lambda *a, **k: {
            "status_code": 0,
            "data": {
                "limit_up_count": 52,
                "limit_down_count": 3,
                "info": [
                    {
                        "code": "600000",
                        "name": "浦发银行",
                        "latest": 11.23,
                        "change_rate": 10.02,
                        "reason_type": "银行",
                        "limit_up_type": "首板",
                        "limit_up_suc_rate": 0.92,
                        "open_num": 1,
                        "order_amount": 1.2e8,
                        "high_days": "2天2板",
                        "first_limit_up_time": "1792718700",
                        "is_again_limit": 0,
                    }
                ],
            },
        },
    )

    out = market.ths_limit_up_pool("2026-10-23")

    row = out["data"][0]
    assert row["first_time"] == "09:25:00"
    assert row["seal_rate"] == 0.92
    assert out["meta"]["limit_up_count"] == 52


def test_concept_blocks_accepts_dict_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        market,
        "em_get",
        lambda *a, **k: {"data": {"diff": {"0": {"f12": "BK0447", "f14": "消费电子", "f3": 1.1, "f128": "立讯精密"}}}},
    )

    out = market.stock_concept_blocks("002475")

    assert out["data"]["total"] == 1
    assert out["data"]["concept_tags"] == ["消费电子"]


def test_dragon_tiger_seats_sums_only_institution_seats(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        {"TRADE_DATE": "2026-04-20 00:00:00", "EXPLANATION": "日涨幅偏离值达到7%", "BILLBOARD_NET_AMT": 502904000.0, "TURNOVERRATE": 5.41}
    ]

    def fake_strict(report: str, *a, **k):
        if report == "RPT_DAILYBILLBOARD_DETAILSNEW":
            return records
        if report == "RPT_BILLBOARD_DAILYDETAILSBUY":
            return [
                {"OPERATEDEPT_NAME": "机构专用", "OPERATEDEPT_CODE": "0", "BUY": 10_000_000, "SELL": 0, "NET": 10_000_000},
                {"OPERATEDEPT_NAME": "深股通", "OPERATEDEPT_CODE": "6", "BUY": 5_000_000, "SELL": 1_000_000, "NET": 4_000_000},
            ]
        return [{"OPERATEDEPT_NAME": "机构专用", "OPERATEDEPT_CODE": "0", "BUY": 0, "SELL": 4_000_000, "NET": -4_000_000}]

    monkeypatch.setattr(market, "_strict_rows", fake_strict)

    out = market.dragon_tiger_seats("002475", trade_date="2026-04-30", look_back=30)

    data = out["data"]
    assert data["latest_date"] == "2026-04-20"
    assert data["institution"] == {"buy_amt": 1000.0, "sell_amt": 400.0, "net_amt": 600.0}
    assert len(data["seats"]["buy"]) == 2


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


def test_lockup_expiry_scales_ratio_and_filters_upcoming(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    row = {
        "SECURITY_CODE": "002475",
        "FREE_DATE": "2026-11-20 00:00:00",
        "FREE_SHARES_TYPE": "定向增发机构配售股份",
        "FREE_SHARES": 1000.0,
        "ABLE_FREE_SHARES": 800.0,
        "FREE_RATIO": 0.0125,
        "LIFT_MARKET_CAP": 35000.0,
    }

    def fake_rows(report: str, filter_str: str, *a, **k):
        assert report == "RPT_LIFT_STAGE"
        calls.append(filter_str)
        return [row]

    monkeypatch.setattr(events, "_event_rows", fake_rows)

    out = events.lockup_expiry("002475", trade_date="2026-10-23", forward_days=90)

    data = out["data"]
    assert data["history"][0]["ratio_pct"] == 1.25
    assert data["history"][0]["able_shares_10k"] == 800.0
    assert "(FREE_DATE>='2026-10-23')(FREE_DATE<='2027-01-21')" in calls[1]
    assert data["upcoming"] == data["history"]


# --------------------------------------------------------------------------- #
# Chip distribution (pure-stdlib simulation)
# --------------------------------------------------------------------------- #


class _Cursor:
    error_code = "0"
    error_msg = ""

    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows
        self._index = -1

    def next(self) -> bool:
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        return self._rows[self._index]


class _FakeBaoStock:
    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows
        self.query: dict[str, object] = {}

    def login(self):
        return types.SimpleNamespace(error_code="0", error_msg="")

    def logout(self) -> None:
        return None

    def query_history_k_data_plus(self, code: str, fields: str, **kwargs):
        self.query = {"code": code, "fields": fields, **kwargs}
        return _Cursor(self._rows)


def _chip_input() -> list[list[str]]:
    return [
        ["2026-10-20", "10.0", "10.0", "10.0", "1.0", "1"],
        ["2026-10-21", "100.0", "100.0", "100.0", "1.0", "1"],
        # Suspended day: must not decay the profile.
        ["2026-10-22", "50.0", "50.0", "50.0", "80.0", "0"],
        ["2026-10-23", "11.0", "10.0", "11.0", "2.0", "1"],
    ]


def test_chip_distribution_seeds_from_the_first_day(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBaoStock(_chip_input())
    monkeypatch.setattr(funds, "_baostock", lambda: fake)
    monkeypatch.setattr(funds, "_bs_code", lambda code: "sz." + code)

    out = funds.chip_distribution("002475", days=60, grid_size=200)

    data = out["data"]
    # One 1% day at 100 can move the 10.0 seed only slightly; starting from
    # zero would land near 55.
    assert data["avg_cost"] < 12
    assert 0 <= data["profit_ratio"] <= 1
    assert data["cost_90"][0] <= data["cost_70"][0] <= data["cost_70"][1] <= data["cost_90"][1]
    assert data["concentration_90"] >= data["concentration_70"]
    assert data["days"] == 3  # the suspended row is dropped
    assert fake.query["adjustflag"] == "2"
    assert fake.query["fields"] == "date,high,low,close,turn,tradestatus"


def test_chip_distribution_rejects_an_empty_window(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBaoStock([["2026-10-23", "10.0", "10.0", "10.0", "1.0", "0"]])
    monkeypatch.setattr(funds, "_baostock", lambda: fake)
    monkeypatch.setattr(funds, "_bs_code", lambda code: "sz." + code)

    with pytest.raises(TickHubError) as exc:
        funds.chip_distribution("002475")

    assert exc.value.code == "QFSD-1002"


# --------------------------------------------------------------------------- #
# Alias
# --------------------------------------------------------------------------- #


def test_em_yzt_pool_points_at_the_yesterday_pool() -> None:
    assert ALIASES["em_yzt_pool"] == "yesterday_limit_up_pool"
    assert resolve("em_yzt_pool").name == "yesterday_limit_up_pool"
