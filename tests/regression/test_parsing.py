"""RG-01..RG-04: golden-shape parsing, unit scaling, board text/HTML, partial batches."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from tickhub_cn.datasources import funds, quotes
from tickhub_cn.datasources.backups import _parse_sse_lhb, _shares, _szse_block, _yuan
from tickhub_cn.datasources.base import run_batch
from tickhub_cn.datasources.util import to_float

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- RG-01


def test_qt_line_fixture_parses_to_public_shape() -> None:
    lines = [line for line in (FIXTURES / "tencent_qt_lines.txt").read_text(encoding="utf-8").splitlines() if line]
    rows = [quotes._parse_qt_line(line) for line in lines]

    assert [row["code"] for row in rows] == ["600000", "000001"]
    sh, sz = rows
    assert sh["name"] == "浦发银行"
    assert sh["market"] == "SH"
    assert sh["price"] == 10.5
    assert sh["prev_close"] == 10.2
    assert sh["change"] == 0.3
    assert sh["change_pct"] == 2.94
    assert sh["high"] == 10.7
    assert sh["low"] == 10.2
    assert sh["volume"] == 123456
    assert sh["amount"] == 98765432.0
    assert sh["pe"] == 6.54
    assert sh["pb"] == 0.72
    assert sh["turnover_rate"] == 1.23
    assert sh["amplitude"] == 2.34
    assert sh["limit_up"] == 11.22
    assert sh["limit_down"] == 9.18
    assert sh["timestamp"] == "20261024150000"
    assert sz["market"] == "SZ"


def test_qt_line_rejects_malformed_input() -> None:
    assert quotes._parse_qt_line("") is None
    assert quotes._parse_qt_line("garbage") is None
    assert quotes._parse_qt_line('v_sh600000="1~短~600000~10.5";') is None


def test_kline_fixture_parses_to_public_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.loads((FIXTURES / "tencent_kline.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(quotes, "request", lambda *a, **k: payload)

    out = quotes.tencent_kline("600000")

    assert out["data"]["code"] == "600000"
    assert out["data"]["period"] == "daily"
    assert out["data"]["adjust"] == "qfq"
    rows = out["data"]["klines"]
    assert len(rows) == 3
    assert rows[0] == {
        "date": "2026-10-20",
        "open": 10.0,
        "close": 10.2,
        "high": 10.4,
        "low": 9.9,
        "volume": 10000.0,
        "amount": 1000.0,
    }
    assert out["meta"]["count"] == 3


# --------------------------------------------------------------------------- RG-02


def test_yuan_scales_from_100m_and_10k() -> None:
    assert _yuan("1.23", 1e8) == 123000000.0
    assert _yuan("23,456.78", 1e4) == 234567800.0
    assert _yuan("", 1e8) is None
    assert _yuan("--", 1e8) is None


def test_shares_scales_from_10k() -> None:
    assert _shares("12.5", 1e4) == 125000.0
    assert _shares(3500, 100) == 350000.0
    assert _shares(None, 1e4) is None


def test_to_float_handles_vendor_quirks() -> None:
    assert to_float("1,234.5") == 1234.5
    assert to_float("12.3%") == 12.3
    assert to_float("-0.8") == -0.8
    assert to_float("--") is None
    assert to_float("停牌") is None
    assert to_float(float("nan")) is None
    assert to_float("", default=0.0) == 0.0


# --------------------------------------------------------------------------- RG-03


def test_sse_lhb_fixed_width_fixture() -> None:
    lines = (FIXTURES / "sse_lhb.txt").read_text(encoding="utf-8").splitlines()
    rows = _parse_sse_lhb(lines)

    assert [row["code"] for row in rows] == ["600000", "600519", "000001", "688981"]
    first = rows[0]
    assert first["name"] == "浦发银行"
    assert first["amount"] == 234567800.0
    assert first["volume"] == 12345678.0
    assert first["reason"] == "主板股票"
    assert first["board"] == "日涨幅偏离值达到7%的证券："
    assert first["period"] == "09-01至09-30"
    assert first["source"] == "sse"
    assert rows[1]["period"] is None
    assert rows[2]["amount"] == 54321000.0
    assert rows[3]["reason"] == "科创板"


def test_szse_block_unwraps_single_element_list() -> None:
    assert _szse_block([{"metadata": {}, "data": ["x"]}]) == {"metadata": {}, "data": ["x"]}
    assert _szse_block([]) == {}
    assert _szse_block({"data": []}) == {}
    assert _szse_block(["nope"]) == {}


def test_etf_share_history_html_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    html = (FIXTURES / "eastmoney_etf_shares.html").read_text(encoding="utf-8")
    monkeypatch.setattr(funds, "request", lambda *a, **k: html)

    out = funds.etf_share_history("510300", page_size=10)

    records = out["data"]["records"]
    assert [row["date"] for row in records] == ["2026-10-22", "2026-10-21", "2026-10-20"]
    assert records[0]["shares"] == 12.34
    assert out["meta"]["count"] == 3
    assert out["meta"]["source"] == "eastmoney"


def test_etf_share_history_respects_page_size(monkeypatch: pytest.MonkeyPatch) -> None:
    html = (FIXTURES / "eastmoney_etf_shares.html").read_text(encoding="utf-8")
    monkeypatch.setattr(funds, "request", lambda *a, **k: html)

    assert len(funds.etf_share_history("510300", page_size=1)["data"]["records"]) == 1


# --------------------------------------------------------------------------- RG-04


def test_run_batch_keeps_successes_and_flags_partial() -> None:
    def work(code: str) -> dict[str, str]:
        if code == "000001":
            raise requests.ConnectionError("blocked")
        return {"code": code}

    payload = run_batch(["600000", "000001", "600519"], work, concurrency=2)

    assert payload["ok"] is False
    assert payload["data"]["total"] == 3
    assert payload["data"]["succeeded"] == 2
    assert payload["data"]["failed"] == 1
    assert set(payload["data"]["codes"]) == {"600000", "600519"}
    assert payload["data"]["failures"][0]["code"] == "000001"
    assert payload["data"]["failures"][0]["error"]["code"] == "QFSD-9003"
    assert payload["meta"]["error_code"] == "QFSD-8001"
    assert payload["meta"]["partial"] is True
    assert payload["meta"]["retryable"] is True


def test_run_batch_rejects_oversized_input() -> None:
    from tickhub_cn.core.errors import TickHubError

    with pytest.raises(TickHubError) as excinfo:
        run_batch([str(i) for i in range(51)], lambda c: c)

    assert excinfo.value.code == "QFSD-9001"


def test_cli_batch_exits_8_on_partial_failure(
    monkeypatch: pytest.MonkeyPatch, run_cli_json
) -> None:
    payload = json.loads((FIXTURES / "tencent_kline.json").read_text(encoding="utf-8"))

    def fake_request(source: str, method: str, url: str, *, params=None, **kwargs):
        if "sz000001" in str((params or {}).get("param", "")):
            raise requests.ConnectionError("blocked")
        return payload

    monkeypatch.setattr(quotes, "request", fake_request)

    code, out, _ = run_cli_json("kline", "batch", "--codes", "600000,000001,600519")

    assert code == 8
    assert out["ok"] is False
    assert out["data"]["succeeded"] == 2
    assert out["data"]["failed"] == 1
    assert out["meta"]["error_code"] == "QFSD-8001"
