"""push2 endpoint-family pacing, reset breaker and stale-cache fallback."""

from __future__ import annotations

import pytest
import requests

from tickhub_cn.core import http as http_mod
from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import base as base_mod


class _NoLimit:
    def acquire(self) -> None:
        pass


class _ResetSession:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        raise requests.ConnectionError("Connection reset by peer")


@pytest.fixture(autouse=True)
def clean_guard():
    http_mod.reset_endpoint_guard()
    with base_mod._LAST_GOOD_LOCK:
        base_mod._LAST_GOOD.clear()
    yield
    http_mod.reset_endpoint_guard()


def _offline(monkeypatch, session):
    monkeypatch.setattr(http_mod, "_session_for", lambda source: session)
    monkeypatch.setattr(http_mod, "limiter_for", lambda source: _NoLimit())
    monkeypatch.setattr(http_mod, "_proxy_for", lambda source, exclude=frozenset(): (None, None))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://push2.eastmoney.com/api/qt/stock/get", "push2.eastmoney.com/api/qt/stock/get"),
        ("https://push2.eastmoney.com/api/qt/ulist.np/get", "push2.eastmoney.com/api/qt/ulist/get"),
        ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
         "push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"),
        ("https://push2ex.eastmoney.com/getTopicZTPool", "push2ex.eastmoney.com/getTopicZTPool"),
        ("https://push2.eastmoney.com/api/qt/kamt/get", None),
        ("https://push2.eastmoney.com/api/qt/slist/get", None),
        ("https://datacenter-web.eastmoney.com/api/data/v1/get", None),
        ("https://www.szse.cn/api/report/ShowReport/data", None),
    ],
)
def test_endpoint_family_groups_quote_paths_only(url, expected):
    assert http_mod.endpoint_family(url) == expected


def test_endpoint_limiter_is_one_call_per_25s():
    limiter = http_mod.endpoint_limiter("push2.eastmoney.com/api/qt/stock/get")
    assert limiter.min_interval == http_mod.ENDPOINT_MIN_INTERVAL == 25.0
    assert http_mod.ENDPOINT_BREAKER_SECONDS == 600.0


def test_first_reset_trips_breaker_without_retrying(monkeypatch):
    session = _ResetSession()
    _offline(monkeypatch, session)
    url = "https://push2.eastmoney.com/api/qt/stock/get"

    with pytest.raises(TickHubError) as first:
        http_mod.fetch("eastmoney", "GET", url, retries=2)
    assert first.value.code == "QFSD-7001"
    assert first.value.detail["retry_after_s"] == 600
    assert session.calls == 1

    with pytest.raises(TickHubError) as second:
        http_mod.fetch("eastmoney", "GET", url, retries=2)
    assert second.value.code == "QFSD-7001"
    assert session.calls == 1
    assert second.value.detail["endpoint"] == "push2.eastmoney.com/api/qt/stock/get"


def test_breaker_is_scoped_to_one_endpoint_family(monkeypatch):
    session = _ResetSession()
    _offline(monkeypatch, session)

    with pytest.raises(TickHubError):
        http_mod.fetch("eastmoney", "GET", "https://push2.eastmoney.com/api/qt/stock/get", retries=0)

    with pytest.raises(TickHubError) as other:
        http_mod.fetch("eastmoney", "GET", "https://push2.eastmoney.com/api/qt/clist/get", retries=0)
    assert other.value.code == "QFSD-7001"
    assert session.calls == 2
    assert "clist/get" in other.value.detail["endpoint"]


def test_non_push2_reset_keeps_previous_behaviour(monkeypatch):
    session = _ResetSession()
    _offline(monkeypatch, session)

    with pytest.raises(TickHubError) as err:
        http_mod.fetch("sina", "GET", "https://finance.sina.com.cn/x", retries=0)
    assert err.value.code == "QFSD-9003"
    assert session.calls == 1
    assert http_mod.ENDPOINT_BREAKER.open_families() == {}


@pytest.fixture
def probe_tool():
    from tickhub_cn.cache.redis_cache import reset_cache

    reset_cache()
    calls = {"n": 0}

    @base_mod.tool(name="_probe_flow", layer="_probe", ttl="realtime", cache_domain="_probe")
    def _probe(code: str = "X") -> dict:
        """Probe tool."""
        calls["n"] += 1
        if calls["n"] > 1:
            raise TickHubError("QFSD-7001", "blocked", detail={"retry_after_s": 600})
        return {"ok": True, "data": {"value": 1}, "meta": {"source": "probe"}}

    try:
        yield _probe, calls
    finally:
        base_mod.REGISTRY.pop("_probe_flow", None)
        base_mod.LAYERS.pop("_probe", None)
        reset_cache()


def test_wrapper_serves_last_good_payload_when_breaker_opens(probe_tool):
    fn, calls = probe_tool
    first = fn(code="X")
    assert first["data"] == {"value": 1}
    assert first["meta"]["cache_hit"] is False
    assert calls["n"] == 1

    stale = fn(code="X", _refresh=True)
    assert calls["n"] == 2
    assert stale["data"] == {"value": 1}
    assert stale["meta"]["stale"] is True
    assert stale["meta"]["degraded"] == "stale_cache"
    assert stale["meta"]["error_code"] == "QFSD-7001"
    assert stale["meta"]["cache_hit"] is True


def test_wrapper_raises_when_nothing_was_ever_cached(probe_tool):
    fn, calls = probe_tool
    calls["n"] = 1  # the first producer call fails
    with pytest.raises(TickHubError) as err:
        fn(code="X")
    assert err.value.code == "QFSD-7001"
