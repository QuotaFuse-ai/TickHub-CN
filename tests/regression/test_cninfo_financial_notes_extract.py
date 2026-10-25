"""cninfo_financial_notes(extract=true): PDF body extraction with a locator fallback."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import announcements, financials

ANNUAL = [
    {"title": "2025年年度报告", "publish_time": "2026-03-31 00:00:00", "url": "http://x/2025.PDF"},
    {"title": "2024年年度报告", "publish_time": "2025-03-29 00:00:00", "url": "http://x/2024.PDF"},
]


def _fake_rows(code: str, start: str, end: str, category: str, page: int, page_size: int, searchkey: str = ""):
    rows = ANNUAL if not searchkey else []
    return [dict(row) for row in rows], len(rows)


def _pdf_bytes(lines: list[str]) -> bytes:
    """A minimal, valid PDF with one Helvetica text line per page."""
    page_ids: list[int] = []
    content_ids: list[int] = []
    next_id = 4
    for _ in lines:
        page_ids.append(next_id)
        content_ids.append(next_id + 1)
        next_id += 2
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{pid} 0 R' for pid in page_ids)}] /Count {len(page_ids)} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for pid, cid, line in zip(page_ids, content_ids, lines):
        objects[pid] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {cid} 0 R >>"
        ).encode()
        stream = f"BT /F1 12 Tf 72 720 Td ({line}) Tj ET".encode()
        objects[cid] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for oid in sorted(objects):
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n".encode() + objects[oid] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for oid in range(1, size):
        out += f"{offsets[oid]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


def test_a_real_pdf_yields_one_text_block_per_page() -> None:
    pages, engine = financials._pdf_pages(_pdf_bytes(["First page body", "Second page body"]))

    assert engine in {"pypdf", "pdftotext"}
    assert len(pages) == 2
    assert "Second page body" in pages[1]


def test_without_any_engine_the_error_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", None)
    monkeypatch.setattr(financials.shutil, "which", lambda _name: None)

    with pytest.raises(TickHubError) as excinfo:
        financials._pdf_pages(b"%PDF-1.4")

    assert excinfo.value.code == "QFSD-9006"
    assert "tickhub-cn[notes]" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Section locator
# --------------------------------------------------------------------------- #


def test_the_table_of_contents_hit_is_skipped_for_the_real_section() -> None:
    pages = [f"第 {i} 页 正文" for i in range(10)]
    pages[1] = "目录 财务报表附注 ........ 120"
    pages[7] = "财务报表附注"
    pages[8] = "一、公司基本情况"

    out = financials._notes_excerpt(pages, 5000)

    assert out["found"] is True
    assert out["heading"] == "财务报表附注"
    assert out["start_page"] == 8
    assert out["text"].startswith("财务报表附注")
    assert "一、公司基本情况" in out["text"]
    assert out["truncated"] is False


def test_a_toc_only_heading_still_returns_something() -> None:
    pages = ["封面", "目录 财务报表附注", "正文", "正文", "正文"]

    out = financials._notes_excerpt(pages, 5000)

    assert out["start_page"] == 2


def test_the_specific_chapter_wins_over_the_audit_report_mention() -> None:
    pages = [f"第 {i} 页 正文" for i in range(30)]
    pages[10] = "一、审计意见 …… 以及相关财务报表附注。"
    pages[20] = "七、合并财务报表项目注释"

    out = financials._notes_excerpt(pages, 5000)

    assert out["heading"] == "合并财务报表项目注释"
    assert out["start_page"] == 21


def test_no_heading_reports_not_found() -> None:
    out = financials._notes_excerpt(["a", "b", "c"], 5000)

    assert out["found"] is False
    assert "未出现" in out["reason"]


def test_a_chapter_opening_wins_over_a_cross_reference_on_another_page() -> None:
    """The audit report cites the notes chapter mid-page, well before it starts."""
    pages = [f"第 {i} 页 正文" for i in range(30)]
    pages[12] = "前文" * 100 + "财务报表附注" + "后文" * 100
    pages[18] = "财务报表附注\n一、公司基本情况"

    out = financials._notes_excerpt(pages, 5000)

    assert out["start_page"] == 19


def test_a_legacy_cid_font_is_reported_as_unreadable() -> None:
    mojibake = ["ᅱഅၿྛٺܢႵཋ܄ඳ" * 400] * 4
    readable = ["招商银行股份有限公司 2025 年度报告 财务报表附注" * 50] * 4

    assert financials._text_layer_readable(mojibake) is False
    assert financials._text_layer_readable(readable) is True
    assert financials._text_layer_readable(["短文"]) is True


def test_the_excerpt_is_cut_to_chars() -> None:
    pages = ["x" * 100, "财务报表附注" + "y" * 500]

    out = financials._notes_excerpt(pages, 120)

    assert len(out["text"]) == 120
    assert out["truncated"] is True


# --------------------------------------------------------------------------- #
# Tool
# --------------------------------------------------------------------------- #


def test_extract_false_never_fetches_the_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(announcements, "_cninfo_rows", _fake_rows)
    monkeypatch.setattr(financials, "fetch", lambda *a, **k: pytest.fail("no PDF fetch expected"))

    out = financials.cninfo_financial_notes("600000")

    assert "notes" not in out["data"]
    assert "附注" in out["meta"]["note"]
    assert "degraded" not in out["meta"]


def test_extract_true_returns_the_notes_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(announcements, "_cninfo_rows", _fake_rows)
    monkeypatch.setattr(financials, "fetch", lambda *a, **k: type("R", (), {"content": b"%PDF-1.4"})())
    monkeypatch.setattr(
        financials,
        "_pdf_pages",
        lambda blob: (
            ["目录 财务报表附注 120"] + ["正文"] * 4 + ["财务报表附注", "（一）货币资金 1,234.00 万元"],
            "pypdf",
        ),
    )

    out = financials.cninfo_financial_notes("600000", extract=True, chars=1000)
    notes = out["data"]["notes"]

    assert notes["found"] is True
    assert notes["heading"] == "财务报表附注"
    assert notes["engine"] == "pypdf"
    assert notes["pages"] == 7
    assert notes["start_page"] == 6
    assert "货币资金" in notes["text"]
    assert notes["url"] == "http://x/2025.PDF"
    assert "degraded" not in out["meta"]
    assert "第 6 页" in out["meta"]["note"]


def test_a_missing_section_degrades_but_keeps_the_locator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(announcements, "_cninfo_rows", _fake_rows)
    monkeypatch.setattr(financials, "fetch", lambda *a, **k: type("R", (), {"content": b"%PDF-1.4"})())
    monkeypatch.setattr(financials, "_pdf_pages", lambda blob: (["封面", "正文"], "pypdf"))

    out = financials.cninfo_financial_notes("600000", extract=True)

    assert out["data"]["notes"]["found"] is False
    assert [row["fiscal_year"] for row in out["data"]["reports"]] == ["2025", "2024"]
    assert out["meta"]["degraded"] == "notes_not_found"


def test_an_unreadable_text_layer_degrades_distinctly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(announcements, "_cninfo_rows", _fake_rows)
    monkeypatch.setattr(financials, "fetch", lambda *a, **k: type("R", (), {"content": b"%PDF-1.4"})())
    monkeypatch.setattr(financials, "_pdf_pages", lambda blob: (["ᅱഅၿྛٺܢႵ܋܄ඳ" * 400] * 4, "pypdf"))

    out = financials.cninfo_financial_notes("600036", extract=True)
    notes = out["data"]["notes"]

    assert (notes["found"], notes["unreadable"]) == (False, True)
    assert out["data"]["reports"][0]["title"] == "2025年年度报告"
    assert out["meta"]["degraded"] == "notes_text_layer_unreadable"
    assert "Adobe-GB1" in out["meta"]["note"]


def test_a_pdf_failure_degrades_but_keeps_the_locator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(announcements, "_cninfo_rows", _fake_rows)

    def blocked(*args: Any, **kwargs: Any):
        raise TickHubError("QFSD-9003", "pdf host blocked")

    monkeypatch.setattr(financials, "fetch", blocked)

    out = financials.cninfo_financial_notes("600000", extract=True)

    assert out["data"]["notes"]["found"] is False
    assert "pdf host blocked" in out["data"]["notes"]["reason"]
    assert out["data"]["reports"][0]["title"] == "2025年年度报告"
    assert out["meta"]["degraded"] == "notes_extract_failed"
    assert "pdf host blocked" in out["meta"]["note"]
