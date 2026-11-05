"""QUOT-369: ``stock_fund_flow_batch`` — 一次 ulist 请求 150 只，超出自动分批。

Nothing here reaches push2 for real: the request count comes from a fake session
(the style of ``tests/contract/test_endpoint_guard.py``) and the payloads from
faked ``em_get`` responses (the style of this directory's other cases).
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from tickhub_cn.core import http as http_mod
from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import funds


class _NoLimit:
    def acquire(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def note_extra_call(self) -> None:
        return None


class _Response:
    status_code = 200
    encoding = "utf-8"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def raise_for_status(self) -> None:
        return None


def _diff(secids: list[str]) -> dict[str, Any]:
    return {
        "rc": 0,
        "data": {
            "total": len(secids),
            "diff": [
                {
                    "f12": secid.split(".")[1],
                    "f14": f"股份{secid.split('.')[1]}",
                    "f2": 10.5,
                    "f3": 1.23,
                    "f62": 1234567.0,
                    "f184": 4.56,
                }
                for secid in secids
            ],
        },
    }


def _codes(count: int) -> list[str]:
    return [f"60{i:04d}" for i in range(1, count + 1)]


class _RecordingSession:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> _Response:
        merged = dict(params or {})
        self.requests.append({"url": url, "params": merged})
        return _Response(_diff(str(merged.get("secids", "")).split(",")))


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> _RecordingSession:
    """Real request plumbing, fake socket, so the batch loop's calls are countable."""
    session = _RecordingSession()
    monkeypatch.setattr(http_mod, "_session_for", lambda source: session)
    monkeypatch.setattr(http_mod, "limiter_for", lambda source: _NoLimit())
    monkeypatch.setattr(http_mod, "endpoint_limiter", lambda family: _NoLimit())
    monkeypatch.setattr(http_mod, "_proxy_for", lambda source, exclude=frozenset(): (None, None))
    monkeypatch.setattr(funds, "_ULIST_BATCH_GAP", 0.0)
    return session


def test_150_codes_cost_one_upstream_request(upstream: _RecordingSession) -> None:
    out = funds.stock_fund_flow_batch(",".join(_codes(150)), _no_cache=True)

    assert len(upstream.requests) == 1
    called = upstream.requests[0]
    assert called["url"] == funds._ULIST
    assert called["params"]["fields"] == "f12,f14,f2,f3,f62,f184"
    assert called["params"]["fltt"] == "2" and called["params"]["invt"] == "2"
    assert len(called["params"]["secids"].split(",")) == 150
    assert called["params"]["secids"].startswith("1.")  # eastmoney_secid: 沪 1.
    assert out["ok"] is True
    assert out["data"]["total"] == 150
    assert out["data"]["succeeded"] == 150
    assert out["data"]["failed"] == 0 and out["data"]["dropped"] == []


def test_200_codes_become_two_requests_with_no_overlap_and_no_loss(upstream: _RecordingSession) -> None:
    codes = _codes(200)

    out = funds.stock_fund_flow_batch(",".join(codes), _no_cache=True)

    sizes = [len(r["params"]["secids"].split(",")) for r in upstream.requests]
    assert sizes == [150, 50]
    secids = [s for r in upstream.requests for s in r["params"]["secids"].split(",")]
    assert len(secids) == len(set(secids)) == 200
    assert sorted(out["data"]["flows"]) == sorted(codes)
    assert out["data"]["dropped"] == [] and out["data"]["failed"] == 0
    assert out["ok"] is True


def test_max_codes_sets_the_slice_size(upstream: _RecordingSession) -> None:
    out = funds.stock_fund_flow_batch(",".join(_codes(120)), max_codes=50, _no_cache=True)

    assert [len(r["params"]["secids"].split(",")) for r in upstream.requests] == [50, 50, 20]
    assert len(out["data"]["flows"]) == 120


def test_a_slice_never_exceeds_the_silent_truncation_limit(upstream: _RecordingSession) -> None:
    funds.stock_fund_flow_batch(",".join(_codes(300)), max_codes=3000, _no_cache=True)

    assert [len(r["params"]["secids"].split(",")) for r in upstream.requests] == [150, 150]


def test_a_failed_batch_is_reported_without_costing_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def fake_em_get(url: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
        secids = str((params or {}).get("secids", ""))
        asked.append(secids)
        if len(asked) == 1:
            raise TickHubError("QFSD-9003", "eastmoney request failed: HTTP 502", detail={"status": 502})
        return _diff(secids.split(","))

    monkeypatch.setattr(funds, "em_get", fake_em_get)
    monkeypatch.setattr(funds, "_ULIST_BATCH_GAP", 0.0)
    codes = _codes(200)

    out = funds.stock_fund_flow_batch(",".join(codes), _no_cache=True)

    assert out["ok"] is False
    assert out["meta"]["error_code"] == "QFSD-8001"
    assert out["data"]["failed"] == 150
    assert out["data"]["succeeded"] == 50
    assert [f["codes"] for f in out["data"]["failures"]] == [codes[:150]]
    assert out["data"]["failures"][0]["error"]["code"] == "QFSD-9003"
    assert sorted(out["data"]["flows"]) == sorted(codes[150:])
    assert len(asked) == 2  # 没有退化成逐只 fan-out


def test_a_paced_endpoint_stops_later_batches_instead_of_stalling(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def fake_em_get(url: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
        secids = str((params or {}).get("secids", ""))
        asked.append(secids)
        raise TickHubError(
            "QFSD-7001",
            "push2.eastmoney.com/api/qt/ulist/get 调用过于频繁",
            detail={"reason": "endpoint_paced", "retry_after_s": 26},
            retryable=True,
        )

    monkeypatch.setattr(funds, "em_get", fake_em_get)
    monkeypatch.setattr(funds, "_ULIST_BATCH_GAP", 0.0)

    out = funds.stock_fund_flow_batch(",".join(_codes(450)), _no_cache=True)

    assert len(asked) == 1
    assert out["data"]["failed"] == 450
    assert [f["batch"] for f in out["data"]["failures"]] == [0, 1, 2]
    assert out["data"]["failures"][1]["error"]["detail"]["retry_after_s"] == 26
    assert out["data"]["flows"] == {}


def test_f62_and_f184_are_published_as_main_net_and_main_net_pct(upstream: _RecordingSession) -> None:
    out = funds.stock_fund_flow_batch("600001,600002", _no_cache=True)

    row = out["data"]["flows"]["600001"]
    assert row["main_net"] == 1234567.0
    assert row["main_net_pct"] == 4.56
    assert row["price"] == 10.5 and row["change_pct"] == 1.23
    assert row["name"] == "股份600001"
    assert "f62" not in row and "f184" not in row


def test_field_names_are_the_ones_realtime_fund_flow_already_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_em_get(url: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
        if url == funds._ULIST:
            return _diff(str((params or {}).get("secids", "")).split(","))
        return {"data": {"f57": "600001", "f58": "股份600001", "f43": 10.5, "f62": 1.0, "f184": 2.0}}

    monkeypatch.setattr(funds, "em_get", fake_em_get)

    single = set(funds.realtime_fund_flow("600001", _no_cache=True)["data"])
    batch = set(funds.stock_fund_flow_batch("600001", _no_cache=True)["data"]["flows"]["600001"])

    assert {"code", "name", "main_net", "main_net_pct"} <= batch & single


def test_an_upstream_row_shortfall_is_listed_not_dropped_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    def short_em_get(url: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
        secids = str((params or {}).get("secids", "")).split(",")
        return _diff(secids[:-2])

    monkeypatch.setattr(funds, "em_get", short_em_get)

    codes = _codes(10)
    out = funds.stock_fund_flow_batch(",".join(codes), _no_cache=True)

    assert out["ok"] is False
    assert out["meta"]["error_code"] == "QFSD-8001"
    assert out["meta"]["requested"] == 10 and out["meta"]["returned"] == 8
    assert out["data"]["dropped"] == codes[-2:]
    assert out["data"]["batches"][0]["dropped"] == codes[-2:]
    assert out["data"]["batches"][0]["requested"] == 10
    assert out["data"]["batches"][0]["returned"] == 8


def test_second_batch_waits_for_the_endpoint_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    stamps: list[float] = []

    def fake_em_get(url: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
        stamps.append(time.monotonic())
        return _diff(str((params or {}).get("secids", "")).split(","))

    monkeypatch.setattr(funds, "em_get", fake_em_get)
    monkeypatch.setattr(funds, "_ULIST_BATCH_GAP", 0.35)

    out = funds.stock_fund_flow_batch(",".join(_codes(200)), _no_cache=True)

    assert len(stamps) == 2
    assert stamps[1] - stamps[0] >= 0.3
    assert len(out["data"]["flows"]) == 200


def test_batch_gap_tracks_the_push2_endpoint_gate_without_touching_it() -> None:
    assert funds._ULIST_BATCH_GAP > http_mod.ENDPOINT_MIN_INTERVAL
    assert funds._ULIST_BATCH_GAP <= http_mod.ENDPOINT_MIN_INTERVAL + 1.0


def test_the_batch_carries_the_realtime_caliber_and_fetch_time(upstream: _RecordingSession) -> None:
    out = funds.stock_fund_flow_batch("600001", _no_cache=True)

    assert out["meta"]["scope"] == "当日实时"
    assert "当日实时" in out["meta"]["note"]
    assert out["meta"]["fetched_at"] == out["data"]["batches"][0]["fetched_at"]
    assert out["meta"]["source"] == "eastmoney"
    assert out["meta"]["cache_hit"] is False


def test_a_failed_batch_is_not_cached_so_the_retry_reaches_the_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def fake_em_get(url: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
        asked.append(str((params or {}).get("secids", "")))
        if len(asked) == 1:
            raise TickHubError("QFSD-7001", "push2 reset", detail={"reason": "endpoint_breaker"})
        return _diff(asked[-1].split(","))

    monkeypatch.setattr(funds, "em_get", fake_em_get)
    codes = ",".join(_codes(3))

    first = funds.stock_fund_flow_batch(codes)
    second = funds.stock_fund_flow_batch(codes)

    assert len(asked) == 2
    assert first["ok"] is False and first["meta"]["cache_hit"] is False
    assert second["ok"] is True and second["meta"]["cache_hit"] is False
    assert sorted(second["data"]["flows"]) == _codes(3)


def test_a_partial_batch_is_not_cached_and_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def short_then_whole(url: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
        secids = str((params or {}).get("secids", "")).split(",")
        asked.append(",".join(secids))
        return _diff(secids[:-1] if len(asked) == 1 else secids)

    monkeypatch.setattr(funds, "em_get", short_then_whole)
    codes = ",".join(_codes(4))

    first = funds.stock_fund_flow_batch(codes)
    second = funds.stock_fund_flow_batch(codes)

    assert len(asked) == 2
    assert first["meta"]["partial"] is True and first["meta"]["cache_hit"] is False
    assert first["data"]["dropped"] == _codes(4)[-1:]
    assert second["ok"] is True and second["meta"]["cache_hit"] is False


def test_the_endpoint_counter_reads_one_request_for_150_and_two_for_200(upstream: _RecordingSession) -> None:
    http_mod.reset_endpoint_guard()
    family = http_mod.endpoint_family(funds._ULIST)

    funds.stock_fund_flow_batch(",".join(_codes(150)), _no_cache=True)
    assert http_mod.ENDPOINT_COUNTERS.snapshot()[family]["attempts"] == 1

    http_mod.reset_endpoint_guard()
    funds.stock_fund_flow_batch(",".join(_codes(200)), _no_cache=True)
    assert http_mod.ENDPOINT_COUNTERS.snapshot()[family]["attempts"] == 2


def test_a_cached_call_is_served_without_an_upstream_request(upstream: _RecordingSession) -> None:
    codes = ",".join(_codes(3))

    first = funds.stock_fund_flow_batch(codes)
    second = funds.stock_fund_flow_batch(codes)

    assert len(upstream.requests) == 1
    assert first["meta"]["cache_hit"] is False
    assert second["meta"]["cache_hit"] is True
    assert sorted(second["data"]["flows"]) == _codes(3)


def test_duplicate_codes_are_only_asked_for_once(upstream: _RecordingSession) -> None:
    out = funds.stock_fund_flow_batch("600001,600001,600002", _no_cache=True)

    assert len(upstream.requests) == 1
    assert len(upstream.requests[0]["params"]["secids"].split(",")) == 2
    assert out["data"]["total"] == 2


@pytest.mark.parametrize("bad", ["", "  ", "not-a-code"])
def test_bad_input_is_a_parameter_error(bad: str) -> None:
    with pytest.raises(TickHubError) as err:
        funds.stock_fund_flow_batch(bad, _no_cache=True)

    assert err.value.code == "QFSD-9001"
