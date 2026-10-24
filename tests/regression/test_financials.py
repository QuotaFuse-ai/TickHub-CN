"""Income-statement normalisation: derived identities and the full payload."""

from __future__ import annotations

from typing import Any

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import financials


def test_income_identities_are_derived_only_from_present_operands() -> None:
    row = financials._derive_income({"total_profit": 100.0, "income_tax": 25.0, "revenue": 400.0, "operating_cost": 150.0})
    assert row["net_profit"] == 75.0
    assert row["gross_profit"] == 250.0

    # A missing operand stays missing; it must not silently become zero.
    partial = financials._derive_income({"total_profit": 100.0, "revenue": 400.0})
    assert "net_profit" not in partial and "gross_profit" not in partial

    # Values the feed does supply are never overwritten.
    supplied = financials._derive_income(
        {"net_profit": 1.0, "total_profit": 100.0, "income_tax": 25.0, "gross_profit": 2.0, "revenue": 4.0, "operating_cost": 1.0}
    )
    assert supplied["net_profit"] == 1.0 and supplied["gross_profit"] == 2.0


def test_financial_all_includes_indicators_and_survives_their_outage(monkeypatch: Any) -> None:
    statement = {
        "REPORT_DATE": "2026-06-30 00:00:00",
        "TOTAL_OPERATE_INCOME": 1000.0,
        "OPERATE_COST": 600.0,
        "TOTAL_PROFIT": 300.0,
        "INCOME_TAX": 75.0,
        "TOTAL_ASSETS": 5000.0,
        "TOTAL_LIABILITIES": 2000.0,
        "TOTAL_EQUITY": 3000.0,
    }
    monkeypatch.setattr(financials, "em_datacenter", lambda *a, **k: [dict(statement)])
    monkeypatch.setattr(
        financials,
        "_indicator_rows",
        lambda *a, **k: [{"report_date": "2026-06-30", "roe": 3.5, "net_profit": 225.0}],
    )
    payload = financials.eastmoney_financial_all("600000", periods=1, _no_cache=True)
    data = payload["data"]
    assert set(data) >= {"balance", "income", "cashflow", "indicators"}
    assert data["income"][0]["net_profit"] == 225.0
    assert data["income"][0]["gross_profit"] == 400.0
    assert data["indicators"][0]["roe"] == 3.5

    def boom(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        raise TickHubError("QFSD-1001", "indicator feed timed out")

    monkeypatch.setattr(financials, "_indicator_rows", boom)
    degraded = financials.eastmoney_financial_all("600000", periods=1, _no_cache=True)
    assert degraded["ok"] is True
    assert degraded["data"]["indicators"] == []
    assert degraded["data"]["income"][0]["net_profit"] == 225.0
