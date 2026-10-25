"""Offline regressions for the F10, Shenwan-history and notes-locator tools.

The F10 fixture is a real ``最新提示`` dossier captured from the TDX binary
channel, so the box-drawing parser is pinned against the upstream layout
instead of a hand-written approximation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import tickhub_cn.datasources  # noqa: F401  (registers every module)
from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import announcements, f10, financials, sw_industry

FIXTURES = Path(__file__).parent / "fixtures"
F10_BODY = (FIXTURES / "f10_000001.txt").read_text(encoding="utf-8")

SW_ROWS = [
    {"code": "600000", "start_date": "1999-11-10", "industry_code": "440101", "update_time": None},
    {"code": "600000", "start_date": "2014-02-21", "industry_code": "480101", "update_time": None},
    {"code": "600000", "start_date": "2021-07-30", "industry_code": "480301", "update_time": None},
]


# --------------------------------------------------------------------------- #
# F10 最新提示
# --------------------------------------------------------------------------- #


def test_box_table_keeps_wrapped_labels_and_footer_text() -> None:
    sections = f10._sections(F10_BODY.splitlines())
    table = [block for block in sections[0]["blocks"] if block["tables"]][0]["tables"][0]

    # Two labels wrap onto a second row; the separator that collapses the
    # frame to one column must not be mistaken for a continuation.
    assert [record["最新主要指标"] for record in table["records"]] == [
        "每股收益(元)",
        "每股净资产(元)",
        "加权净资产收益率(%)",
        "实际流通A股(万股)",
        "限售流通A股(万股)",
        "总股本(万股)",
    ]
    assert any(line.startswith("●最新公告:") for line in table["text"])
    assert any(line.startswith("●股东人数:") for line in table["text"])


def test_stock_f10_tips_reads_the_captured_dossier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f10, "_f10_body", lambda code: (F10_BODY, ["最新提示"], "最新提示"))

    out = f10.stock_f10_tips("000001")
    data = out["data"]

    assert data["code"] == "000001"
    assert data["name"] == "平安银行"
    assert data["updated"] == "2026-10-24"
    assert data["category"] == "最新提示"
    assert data["business"].startswith("（一）吸收公众存款")
    assert [item["title"] for item in data["announcements"]] == [
        "平安银行（000001）：2026年三季度报告",
        "平安银行（000001）：董事会决议公告",
        "平安银行（000001）：关联交易公告",
    ]
    assert data["announcements"][0]["url"].endswith(".PDF")
    assert len(data["news"]) == 2
    assert [section["index"] for section in data["sections"]] == [1, 2, 3, 4]
    assert out["meta"]["count"] == 4


def test_stock_f10_tips_rejects_beijing_codes() -> None:
    with pytest.raises(TickHubError) as excinfo:
        f10.stock_f10_tips("830799")
    assert excinfo.value.code == "QFSD-9002"


# --------------------------------------------------------------------------- #
# Shenwan classification history
# --------------------------------------------------------------------------- #


def test_sw_history_derives_levels_and_as_of(monkeypatch: pytest.MonkeyPatch) -> None:
    # A repeated row must not become a fake industry change.
    monkeypatch.setattr(sw_industry, "_load_rows", lambda: [*SW_ROWS, dict(SW_ROWS[-1])])

    out = sw_industry.sw_industry_history("600000")
    data = out["data"]
    assert data["total"] == 3
    assert data["first_seen"] == "1999-11-10"
    assert data["current"]["industry_code"] == "480301"
    assert data["current"]["l1_code"] == "480000"
    assert data["current"]["l2_code"] == "480300"
    assert data["current"]["l3_code"] == "480301"
    assert [item["current"] for item in data["history"]] == [False, False, True]

    past = sw_industry.sw_industry_history("600000", as_of="2015-01-01")["data"]
    assert past["current"]["industry_code"] == "480101"
    assert past["current"]["l2_code"] == "480100"
    assert past["last_changed"] == "2014-02-21"

    before = sw_industry.sw_industry_history("600000", as_of="1990-01-01")["data"]
    assert before["current"] is None
    assert before["history"] == []

    tail = sw_industry.sw_industry_history("600000", limit=1)["data"]
    assert tail["total"] == 3
    assert [item["industry_code"] for item in tail["history"]] == ["480301"]


def test_sw_history_returns_empty_list_for_unclassified_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sw_industry, "_load_rows", lambda: list(SW_ROWS))

    out = sw_industry.sw_industry_history("830799")

    assert out["data"] == []
    assert "不在申万行业分类表中" in out["meta"]["note"]


# --------------------------------------------------------------------------- #
# Financial-statement notes locator
# --------------------------------------------------------------------------- #


def test_financial_notes_keep_only_annual_and_audit_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    annual = [
        {"title": "2025年年度报告", "publish_time": "2026-03-31 00:00:00", "url": "https://x/2025.PDF"},
        {"title": "2025年年度报告摘要", "publish_time": "2026-03-31 00:00:00", "url": "https://x/2025s.PDF"},
        {"title": "2025年年度报告（英文版）", "publish_time": "2026-03-31 00:00:00", "url": "https://x/2025e.PDF"},
        {"title": "2024年年度报告", "publish_time": "2025-03-29 00:00:00", "url": "https://x/2024.PDF"},
        {"title": "2025年第三季度报告", "publish_time": "2025-10-25 00:00:00", "url": "https://x/q3.PDF"},
    ]
    audit = [{"title": "2025年度审计报告", "publish_time": "2026-03-31 00:00:00", "url": "https://x/audit.PDF"}]

    def fake_rows(
        code: str, start: str, end: str, category: str, page: int, page_size: int, searchkey: str = "",
    ) -> tuple[list[dict[str, Any]], int]:
        rows = audit if searchkey else annual
        return list(rows), len(rows)

    monkeypatch.setattr(announcements, "_cninfo_rows", fake_rows)

    out = financials.cninfo_financial_notes("600000")
    data = out["data"]
    assert [row["fiscal_year"] for row in data["reports"]] == ["2025", "2024"]
    assert data["reports"][0]["url"] == "https://x/2025.PDF"
    assert data["audit_reports"][0]["title"] == "2025年度审计报告"
    assert "附注" in out["meta"]["note"]

    only_2024 = financials.cninfo_financial_notes("600000", year="2024")["data"]
    assert [row["fiscal_year"] for row in only_2024["reports"]] == ["2024"]
    assert only_2024["audit_reports"] == []


def test_financial_notes_reject_short_year() -> None:
    with pytest.raises(TickHubError) as excinfo:
        financials.cninfo_financial_notes("600000", year="24")
    assert excinfo.value.code == "QFSD-9001"


def test_financial_notes_empty_when_no_annual_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(announcements, "_cninfo_rows", lambda *a, **k: ([], 0))

    out = financials.cninfo_financial_notes("600000", year="2024")

    assert out["data"] == []
    assert "2024 年度" in out["meta"]["note"]
