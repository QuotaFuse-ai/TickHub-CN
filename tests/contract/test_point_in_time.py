"""CT-06: ``data_as_of`` drops later rows and echoes the cutoff."""

from __future__ import annotations

import pytest

from tickhub_cn.core.provenance import as_of_filter
from tickhub_cn.datasources import quotes
from tests.support import kline_payload


def test_data_as_of_trims_later_kline_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quotes, "request", lambda *a, **k: kline_payload(bars=4))
    payload = quotes.tencent_kline("600000", data_as_of="2026-10-21")

    assert [row["date"] for row in payload["data"]["klines"]] == ["2026-10-20", "2026-10-21"]
    assert payload["meta"]["data_as_of"] == "2026-10-21"
    assert payload["meta"]["cache_hit"] in (False, True)


def test_data_as_of_does_not_widen_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quotes, "request", lambda *a, **k: kline_payload(bars=2))
    payload = quotes.tencent_kline("600000", data_as_of="2020-01-01")

    assert payload["data"]["klines"] == []
    assert payload["ok"] is True


def test_rows_without_a_timestamp_are_kept() -> None:
    rows = [{"code": "600000"}, {"date": "2026-10-25"}, {"date": "2026-10-20"}]
    kept = as_of_filter(rows, "2026-10-21T09:30:00+08:00")

    assert [r.get("date") for r in kept] == [None, "2026-10-20"]


def test_publish_time_wins_over_date() -> None:
    rows = [{"date": "2026-10-01", "publish_time": "2026-10-25 08:00:00"}]
    assert as_of_filter(rows, "2026-10-21") == []


def test_cutoff_truncates_to_seconds() -> None:
    rows = [{"publish_time": "2026-10-21 09:30:00"}, {"publish_time": "2026-10-21 09:31:00"}]
    assert len(as_of_filter(rows, "2026-10-21T09:30:59+08:00")) == 1


def test_empty_cutoff_is_a_noop() -> None:
    rows = [{"date": "2099-01-01"}]
    assert as_of_filter(rows, "") == rows
