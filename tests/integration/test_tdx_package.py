"""TDX after-hours package (whole-market daily bars).

The package is a zip of fixed 32-byte records. These tests pin the behaviours
that were previously wrong: the archive is read as bytes, the requested date's
record is located (not just each file's newest one), the cache is keyed on the
trade date, and a date the package does not hold is reported instead of coming
back as a silent empty list.
"""

from __future__ import annotations

import io
import json
import struct
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import quotes


class _Resp:
    def __init__(self, content: bytes = b"", status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code


def _record(date: str, close: float, amount: float = 12345.5) -> bytes:
    ymd = int(date.replace("-", ""))
    return struct.pack("<IIIIIfII", ymd, 1000, 1200, 900, int(round(close * 100)), amount, 100, 0)


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, blob in files.items():
            zf.writestr(name, blob)
    return buf.getvalue()


def _serve(monkeypatch: pytest.MonkeyPatch, payloads: dict[str, Any]) -> list[str]:
    calls: list[str] = []

    def fake(source: str, method: str, url: str, **kwargs: Any) -> _Resp:
        market = url.rsplit("/", 1)[-1].replace("lday.zip", "")
        calls.append(market)
        payload = payloads[market]
        return payload if isinstance(payload, _Resp) else _Resp(payload)

    monkeypatch.setattr(quotes, "fetch", fake)
    return calls


def test_reads_the_requested_date_not_the_newest_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = _zip(
        {
            "sh600000.day": _record("2025-10-22", 10.0)
            + _record("2025-10-23", 11.0)
            + _record("2025-10-24", 12.0),
            "sh000001.day": _record("2025-10-24", 3300.0),
        }
    )
    _serve(monkeypatch, {"sh": payload})

    out = quotes.tdx_daily_package(date="2025-10-23", market="sh", download_dir=str(tmp_path))

    assert [(r["code"], r["close"]) for r in out["data"]["rows"]] == [("600000", 11.0)]
    assert out["data"]["date"] == "2025-10-23"
    assert out["meta"]["count"] == 1
    assert out["meta"]["data_as_of"] == "2025-10-23"
    assert "degraded" not in out["meta"]


def test_empty_date_uses_the_newest_trade_date(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = _zip({"sz000001.day": _record("2025-10-23", 5.0) + _record("2025-10-24", 6.0)})
    _serve(monkeypatch, {"sz": payload})

    out = quotes.tdx_daily_package(date="", market="sz", download_dir=str(tmp_path))

    assert out["data"]["date"] == "2025-10-24"
    assert [r["close"] for r in out["data"]["rows"]] == [6.0]
    assert out["meta"]["data_as_of"] == "2025-10-24"


def test_index_files_are_flagged_and_ranked_after_stocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = _zip(
        {
            "sh999999.day": _record("2025-10-24", 3300.0, amount=9e9),
            "sh600000.day": _record("2025-10-24", 12.0, amount=1e6),
        }
    )
    _serve(monkeypatch, {"sh": payload})

    out = quotes.tdx_daily_package(date="2025-10-24", market="sh", download_dir=str(tmp_path))

    rows = out["data"]["rows"]
    assert [r["code"] for r in rows] == ["600000", "999999"]
    assert [r["is_index"] for r in rows] == [False, True]


def test_a_date_the_package_does_not_hold_is_marked_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _serve(monkeypatch, {"sh": _zip({"sh600000.day": _record("2025-10-24", 12.0)})})

    out = quotes.tdx_daily_package(date="2025-10-25", market="sh", download_dir=str(tmp_path))

    assert out["data"]["rows"] == []
    assert out["data"]["date"] == "2025-10-25"
    assert out["meta"]["degraded"] == "tdx_package_stale"
    assert out["meta"]["data_as_of"] == "2025-10-24"
    assert "2025-10-25" in out["meta"]["note"]


def test_the_archive_is_cached_as_bytes_and_reused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = _zip({"sh600000.day": _record("2025-10-24", 12.0)})
    calls = _serve(monkeypatch, {"sh": payload})
    cache = tmp_path / "tdx"

    first = quotes.tdx_daily_package(date="2025-10-24", market="sh", download_dir=str(cache))

    # A text round-trip used to mangle the zip on disk.
    assert (cache / "shlday.zip").read_bytes() == payload
    stamp = json.loads((cache / "shlday.zip.stamp").read_text(encoding="utf-8"))
    assert stamp == {"downloaded_on": quotes._bj_date(), "covered_through": "2025-10-24"}

    monkeypatch.setattr(quotes, "fetch", lambda *a, **k: pytest.fail("cache should answer"))
    again = quotes.tdx_daily_package(date="2025-10-24", market="sh", download_dir=str(cache))
    earlier = quotes.tdx_daily_package(date="2025-10-20", market="sh", download_dir=str(cache))

    assert again["data"] == first["data"]
    assert earlier["data"]["rows"] == []
    assert calls == ["sh"]


def test_a_stale_cache_is_refreshed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = tmp_path / "tdx"
    cache.mkdir()
    (cache / "shlday.zip").write_bytes(_zip({"sh600000.day": _record("2025-10-23", 11.0)}))
    (cache / "shlday.zip.stamp").write_text(
        json.dumps({"downloaded_on": "2025-10-23", "covered_through": "2025-10-23"}),
        encoding="utf-8",
    )
    fresh = _zip({"sh600000.day": _record("2025-10-23", 11.0) + _record("2025-10-24", 12.0)})
    calls = _serve(monkeypatch, {"sh": fresh})

    out = quotes.tdx_daily_package(date="2025-10-24", market="sh", download_dir=str(cache))

    assert calls == ["sh"]
    assert [r["close"] for r in out["data"]["rows"]] == [12.0]


def test_a_non_zip_payload_is_rejected_before_caching(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _serve(monkeypatch, {"sh": b"<html>maintenance</html>"})
    cache = tmp_path / "tdx"

    with pytest.raises(TickHubError) as err:
        quotes.tdx_daily_package(date="2025-10-24", market="sh", download_dir=str(cache))

    assert err.value.code == "QFSD-9003"
    assert "zip" in err.value.message
    assert not (cache / "shlday.zip").exists()


def test_a_corrupt_cached_archive_is_dropped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = tmp_path / "tdx"
    cache.mkdir()
    good = _zip({"sh600000.day": _record("2025-10-24", 12.0)})
    (cache / "shlday.zip").write_bytes(good[:40])
    (cache / "shlday.zip.stamp").write_text(
        json.dumps({"downloaded_on": quotes._bj_date(), "covered_through": "2025-10-24"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(quotes, "fetch", lambda *a, **k: pytest.fail("the cached file should be used"))

    with pytest.raises(TickHubError) as err:
        quotes.tdx_daily_package(date="2025-10-24", market="sh", download_dir=str(cache))

    assert err.value.code == "QFSD-9003"
    assert "损坏" in err.value.message
    assert not (cache / "shlday.zip").exists()
    assert not (cache / "shlday.zip.stamp").exists()


def test_all_markets_tolerates_a_missing_bse_package(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _serve(
        monkeypatch,
        {
            "sh": _zip({"sh600000.day": _record("2025-10-24", 12.0)}),
            "sz": _zip({"sz000001.day": _record("2025-10-24", 6.0)}),
            "bj": _Resp(b"not found", 404),
        },
    )

    out = quotes.tdx_daily_package(date="2025-10-24", market="all", download_dir=str(tmp_path))

    assert {r["market"] for r in out["data"]["rows"]} == {"SH", "SZ"}
    assert out["meta"]["missing_markets"] == ["bj"]
    assert out["meta"]["degraded"] == "tdx_package_missing"


def test_an_explicit_market_404_is_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _serve(monkeypatch, {"bj": _Resp(b"missing", 404)})

    with pytest.raises(TickHubError) as err:
        quotes.tdx_daily_package(date="2025-10-24", market="bj", download_dir=str(tmp_path))

    assert err.value.code == "QFSD-9003"


def test_bad_arguments_fail_before_any_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(quotes, "fetch", lambda *a, **k: pytest.fail("no download for a bad argument"))

    with pytest.raises(TickHubError) as market_err:
        quotes.tdx_daily_package(date="2025-10-24", market="hk", download_dir=str(tmp_path))
    assert market_err.value.code == "QFSD-9001"

    with pytest.raises(TickHubError) as date_err:
        quotes.tdx_daily_package(date="2025/10/24", market="sh", download_dir=str(tmp_path))
    assert date_err.value.code == "QFSD-9001"
