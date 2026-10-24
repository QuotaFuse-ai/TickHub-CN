"""Regressions for the Eastmoney F10 ownership and main-business tools.

The upstream payloads are pinned here because three details are easy to get
wrong and silent when they are: F10 ratios are fractions (not percents),
``MAINOP_TYPE`` 1 and 2 both carry a product/segment breakdown, and the
``RPT_F10_FN_MAINOP`` gross-profit column is spelled ``RPOFIT``.
"""

from __future__ import annotations

from typing import Any

import pytest

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import financials


def _mainop_row(
    report_date: str,
    type_code: str,
    item: str,
    rank: int,
    *,
    income: float,
    ratio: float,
    margin: float,
) -> dict[str, Any]:
    return {
        "REPORT_DATE": report_date,
        "REPORT_NAME": "2026中报" if report_date.startswith("2026-06") else "2026一季报",
        "SECURITY_NAME_ABBR": "立讯精密",
        "MAINOP_TYPE": type_code,
        "ITEM_NAME": item,
        "MAIN_BUSINESS_INCOME": income,
        "MAIN_BUSINESS_COST": income * (1 - margin),
        "MAIN_BUSINESS_RPOFIT": income * margin,
        "MBI_RATIO": ratio,
        "GROSS_RPOFIT_RATIO": margin,
        "RANK": rank,
    }


def test_main_business_buckets_types_and_converts_fraction_ratios(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _mainop_row("2026-06-30 00:00:00", "1", "消费电子", 1, income=800.0, ratio=0.8, margin=0.2),
        _mainop_row("2026-06-30 00:00:00", "1", "汽车互联", 2, income=200.0, ratio=0.2, margin=0.1),
        _mainop_row("2026-06-30 00:00:00", "2", "连接器", 1, income=600.0, ratio=0.6, margin=0.25),
        _mainop_row("2026-06-30 00:00:00", "2", "声学", 2, income=400.0, ratio=0.4, margin=0.15),
        _mainop_row("2026-03-31 00:00:00", "1", "消费电子", 1, income=700.0, ratio=0.7, margin=0.18),
    ]
    seen: list[dict[str, Any]] = []

    def fake(_report: str, **kwargs: Any) -> list[dict[str, Any]]:
        seen.append(kwargs)
        return rows

    monkeypatch.setattr(financials, "em_datacenter", fake)

    out = financials.main_business("002475", periods=2, _no_cache=True)
    data = out["data"]

    assert data["code"] == "002475"
    assert data["name"] == "立讯精密"
    assert [entry["report_date"] for entry in data["reports"]] == ["2026-06-30", "2026-03-31"]
    latest = data["reports"][0]
    assert latest["report_name"] == "2026中报"
    # Ratios arrive as fractions and must be scaled once, not twice.
    assert latest["by_industry"][0]["revenue_ratio"] == 80.0
    assert latest["by_industry"][0]["gross_margin"] == 20.0
    assert latest["by_product"][0]["item"] == "连接器"
    assert latest["by_product"][0]["gross_profit"] == 150.0
    assert "by_region" not in latest
    assert seen[0]["sort_columns"] == "REPORT_DATE,MAINOP_TYPE"


def test_main_business_keeps_banks_that_only_report_type_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """Banks publish their segment split as type 2 with no type-1 rows."""
    rows = [
        _mainop_row("2026-06-30 00:00:00", "2", "零售金融", 1, income=500.0, ratio=0.5, margin=0.4),
        _mainop_row("2026-06-30 00:00:00", "2", "批发金融", 2, income=500.0, ratio=0.5, margin=0.35),
        _mainop_row("2026-06-30 00:00:00", "3", "总行", 1, income=1000.0, ratio=1.0, margin=0.38),
    ]
    monkeypatch.setattr(financials, "em_datacenter", lambda *a, **k: rows)

    entry = financials.main_business("600036", _no_cache=True)["data"]["reports"][0]

    assert [item["item"] for item in entry["by_product"]] == ["零售金融", "批发金融"]
    assert entry["by_region"][0]["item"] == "总行"
    assert "by_industry" not in entry


def test_main_business_sorts_by_rank_and_reports_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _mainop_row("2026-06-30 00:00:00", "2", "第二", 2, income=1.0, ratio=0.1, margin=0.1),
        _mainop_row("2026-06-30 00:00:00", "2", "第一", 1, income=9.0, ratio=0.9, margin=0.1),
    ]
    monkeypatch.setattr(financials, "em_datacenter", lambda *a, **k: rows)
    entry = financials.main_business("002475", _no_cache=True)["data"]["reports"][0]
    assert [item["item"] for item in entry["by_product"]] == ["第一", "第二"]

    monkeypatch.setattr(financials, "em_datacenter", lambda *a, **k: [])
    out = financials.main_business("002475", _no_cache=True)
    assert out["ok"] is True and out["data"] == []
    assert out["meta"]["note"]


def test_top_shareholders_total_carries_ratios_and_institution_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "END_DATE": "2026-06-30 00:00:00",
            "SECURITY_NAME_ABBR": "立讯精密",
            "HOLDER_RANK": 2,
            "HOLDER_NAME": "香港中央结算有限公司",
            "HOLD_NUM": 50000.0,
            "HOLD_NUM_RATIO": 0.7,
            "HOLD_NUM_CHANGE": "-1200",
            "CHANGE_RATIO": -0.02,
            "IS_HOLDORG": "1",
        },
        {
            "END_DATE": "2026-06-30 00:00:00",
            "SECURITY_NAME_ABBR": "立讯精密",
            "HOLDER_RANK": 1,
            "HOLDER_NAME": "立讯有限公司",
            "HOLD_NUM": 900000.0,
            "HOLD_NUM_RATIO": 12.5,
            "HOLD_NUM_CHANGE": "不变",
            "CHANGE_RATIO": None,
            "IS_HOLDORG": "0",
        },
        {
            "END_DATE": "2026-03-31 00:00:00",
            "SECURITY_NAME_ABBR": "立讯精密",
            "HOLDER_RANK": 1,
            "HOLDER_NAME": "立讯有限公司",
            "HOLD_NUM": 900000.0,
            "HOLD_NUM_RATIO": 12.4,
            "HOLD_NUM_CHANGE": "不变",
            "CHANGE_RATIO": None,
            "IS_HOLDORG": "0",
        },
    ]
    monkeypatch.setattr(financials, "em_datacenter", lambda *a, **k: rows)

    data = financials.top_shareholders("002475", periods=2, _no_cache=True)["data"]

    assert data["holder_type"] == "total"
    assert [entry["report_date"] for entry in data["reports"]] == ["2026-06-30", "2026-03-31"]
    holders = data["reports"][0]["holders"]
    assert [h["rank"] for h in holders] == [1, 2]
    # HOLD_NUM_RATIO is already a percentage; scaling it again would be wrong.
    assert holders[0]["hold_ratio"] == 12.5
    assert holders[0]["is_institution"] is False
    assert holders[1]["is_institution"] is True
    assert holders[1]["change"] == "-1200"
    assert "float_ratio" not in holders[0]


def test_top_shareholders_float_variant_uses_free_float_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "END_DATE": "2026-06-30 00:00:00",
            "SECURITY_NAME_ABBR": "立讯精密",
            "HOLDER_RANK": 1,
            "HOLDER_NAME": "某基金",
            "HOLD_NUM": 4200.0,
            "HOLD_RATIO": 3.1,
            "FREE_HOLDNUM_RATIO": 4.2,
            "HOLDER_TYPE": "基金",
            "HOLD_NUM_CHANGE": "新进",
            "CHANGE_RATIO": None,
            "IS_HOLDORG": "1",
        }
    ]
    seen: list[dict[str, Any]] = []

    def fake(report: str, **kwargs: Any) -> list[dict[str, Any]]:
        seen.append({"report": report, **kwargs})
        return rows

    monkeypatch.setattr(financials, "em_datacenter", fake)

    data = financials.top_shareholders("002475", holder_type="FLOAT", _no_cache=True)["data"]

    assert seen[0]["report"] == "RPT_F10_EH_FREEHOLDERS"
    assert data["holder_type"] == "float"
    holder = data["reports"][0]["holders"][0]
    assert holder["hold_ratio"] == 3.1
    assert holder["float_ratio"] == 4.2
    assert holder["holder_type"] == "基金"


def test_top_shareholders_rejects_unknown_kind_and_index_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(financials, "em_datacenter", lambda *a, **k: [])
    with pytest.raises(TickHubError) as exc:
        financials.top_shareholders("002475", holder_type="board", _no_cache=True)
    assert exc.value.code == "QFSD-9001"

    # sh000001 is a Shanghai index; returning another entity's holders is worse
    # than refusing.
    with pytest.raises(TickHubError) as exc:
        financials.top_shareholders("sh000001", _no_cache=True)
    assert exc.value.code == "QFSD-9001"


def test_top_shareholders_empty_payload_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(financials, "em_datacenter", lambda *a, **k: [])
    out = financials.top_shareholders("002475", _no_cache=True)
    assert out["ok"] is True and out["data"] == []
    assert out["meta"]["note"]
