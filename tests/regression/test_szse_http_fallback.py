"""SZSE plain-HTTP fallback is opt-in, reset-only, and flagged in ``meta``.

The exchange resets some TLS connections. Falling back to port 80 recovers the
tool without any network change, but plain HTTP can be tampered with, so the
switch defaults off and every downgraded result says so.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.core.http import is_connection_reset
from tickhub_cn.datasources import backups


@pytest.fixture(autouse=True)
def _clean_transport() -> None:
    backups._reset_szse_transport()
    yield
    backups._reset_szse_transport()


def _settings(enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(szse_allow_http=enabled)


def _reset_error(url: str) -> TickHubError:
    return TickHubError(
        "QFSD-9003",
        "exchange request failed: ('Connection aborted.', ConnectionResetError(10054, 'reset by peer'))",
        detail={"url": url, "error": "ConnectionError"},
        retryable=True,
    )


# --------------------------------------------------------------------------- #
# Transport classification
# --------------------------------------------------------------------------- #


def test_reset_classifier_understands_boxed_errors() -> None:
    assert is_connection_reset(_reset_error("https://www.szse.cn/x")) is True
    assert is_connection_reset(ConnectionResetError("reset by peer")) is True
    assert is_connection_reset(TickHubError("QFSD-1001", "timeout", detail={"error": "Timeout"})) is False
    assert is_connection_reset(TickHubError("QFSD-9003", "500 Server Error", detail={"error": "HTTPError"})) is False


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #


def test_downgrade_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_request(_source: str, _method: str, url: str, **_kwargs: object) -> object:
        calls.append(url)
        raise _reset_error(url)

    monkeypatch.setattr(backups, "request", fake_request)
    monkeypatch.setattr(backups, "get_settings", lambda: _settings(False))

    with pytest.raises(TickHubError):
        backups._szse_request("GET", backups._SZSE_REPORT)

    assert calls == [backups._SZSE_REPORT]


def test_report_pages_get_a_timeout_that_fits_their_size(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def fake_request(_source: str, _method: str, _url: str, **kwargs: object) -> object:
        seen.append(kwargs.get("timeout"))
        return {"ok": True}

    monkeypatch.setattr(backups, "request", fake_request)

    backups._szse_request("GET", backups._SZSE_REPORT)
    backups._szse_request("GET", backups._SZSE_REPORT, timeout=5)

    assert seen == [backups._SZSE_TIMEOUT, 5]
    assert backups._SZSE_TIMEOUT > 15.0


def test_downgrade_retries_once_over_plain_http(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_request(_source: str, _method: str, url: str, **_kwargs: object) -> object:
        calls.append(url)
        if url.startswith("https://"):
            raise _reset_error(url)
        return {"ok": True}

    monkeypatch.setattr(backups, "request", fake_request)
    monkeypatch.setattr(backups, "get_settings", lambda: _settings(True))

    out = backups._szse_request("GET", backups._SZSE_REPORT)

    assert out == {"ok": True}
    assert calls == [backups._SZSE_REPORT, backups._SZSE_REPORT.replace("https://", "http://")]
    assert backups._szse_transport() == {"szse_transport": "http"}


def test_non_reset_failures_never_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_request(_source: str, _method: str, url: str, **_kwargs: object) -> object:
        calls.append(url)
        raise TickHubError("QFSD-9003", "500 Server Error", detail={"url": url, "error": "HTTPError"})

    monkeypatch.setattr(backups, "request", fake_request)
    monkeypatch.setattr(backups, "get_settings", lambda: _settings(True))

    with pytest.raises(TickHubError):
        backups._szse_request("GET", backups._SZSE_REPORT)

    assert calls == [backups._SZSE_REPORT]
    assert backups._szse_transport() == {}


# --------------------------------------------------------------------------- #
# Tool-level annotations
# --------------------------------------------------------------------------- #

SZ_MARGIN_ROW = {
    "zqdm": "000001",
    "zqjc": "平安银行",
    "jrrzye": 12.5,
    "jrrzmr": 1.2,
    "jrrjye": 0.3,
    "jrrjyl": 4.0,
    "jrrjmc": 0.5,
    "jrrzrjye": 12.8,
}
SZ_MARGIN_SUMMARY = dict(SZ_MARGIN_ROW)


def test_margin_backup_flags_the_plain_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    def fake_request(_source: str, _method: str, url: str, **kwargs: object) -> object:
        urls.append(url)
        if url.startswith("https://"):
            raise _reset_error(url)
        params = kwargs.get("params") or {}
        data = [SZ_MARGIN_ROW] if params.get("TABKEY") == "tab2" else [SZ_MARGIN_SUMMARY]
        return [{"metadata": {"pagecount": 1}, "data": data}]

    monkeypatch.setattr(backups, "request", fake_request)
    monkeypatch.setattr(backups, "get_settings", lambda: _settings(True))

    out = backups.margin_trading_backup(trade_date="2026-10-22", exchange="SZSE")

    assert out["meta"]["source"] == "szse"
    assert out["meta"]["szse_transport"] == "http"
    assert "SZSE_ALLOW_HTTP" in out["meta"]["note"]
    assert out["data"]["records"][0]["code"] == "000001"
    assert any(url.startswith("http://www.szse.cn/") for url in urls)


def test_margin_backup_stays_https_when_the_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(_source: str, _method: str, url: str, **_kwargs: object) -> object:
        if url.startswith("https://"):
            raise _reset_error(url)
        pytest.fail("plain HTTP must not be attempted when the switch is off")

    monkeypatch.setattr(backups, "request", fake_request)
    monkeypatch.setattr(backups, "get_settings", lambda: _settings(False))

    with pytest.raises(TickHubError):
        backups.margin_trading_backup(trade_date="2026-10-22", exchange="SZSE")


def test_announcements_fallback_keeps_https_pdf_links(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(_source: str, _method: str, url: str, **_kwargs: object) -> object:
        if url.startswith("https://"):
            raise _reset_error(url)
        return {
            "data": [
                {
                    "attachPath": "/finalpage/2026-10-24/1.pdf",
                    "secCode": ["000001"],
                    "secName": ["平安银行"],
                    "title": "<em>三季度报告</em>",
                    "attachFormat": "PDF",
                    "attachSize": 120,
                    "publishTime": "2026-10-24 00:00:00",
                }
            ]
        }

    monkeypatch.setattr(backups, "request", fake_request)
    monkeypatch.setattr(backups, "get_settings", lambda: _settings(True))

    out = backups.announcements_backup("000001", page_size=5)

    assert out["meta"]["szse_transport"] == "http"
    item = out["data"]["announcements"][0]
    assert item["title"] == "三季度报告"
    assert str(item["pdf"]).startswith("https://disc.static.szse.cn/download")


def test_dragon_tiger_flags_the_plain_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(_source: str, _method: str, url: str, **_kwargs: object) -> object:
        if url.startswith("https://www.szse.cn/"):
            raise _reset_error(url)
        if url.endswith("showTradePublicFile.do"):
            return {"fileContents": []}
        return [
            {
                "metadata": {"pagecount": 1},
                "data": [{"zqdm": "000001", "zqjc": "平安银行", "cjje": 1.5, "cjsl": 2.0, "plyy": "日涨幅偏离值达7%"}],
            }
        ]

    monkeypatch.setattr(backups, "request", fake_request)
    monkeypatch.setattr(backups, "get_settings", lambda: _settings(True))

    out = backups.dragon_tiger_backup(trade_date="2026-10-22")

    assert out["meta"]["szse_transport"] == "http"
    assert "SZSE_ALLOW_HTTP" in out["meta"]["note"]
    assert out["data"]["szse"][0]["code"] == "000001"
