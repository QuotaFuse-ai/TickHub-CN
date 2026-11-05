"""push2 endpoint-family pacing, reset breaker and stale-cache fallback."""

from __future__ import annotations

import time

import pytest
import requests

from tickhub_cn.core import http as http_mod
from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import base as base_mod
from tickhub_cn.proxy.pool import ProxyNode, ProxyPool


class _NoLimit:
    def acquire(self, *_args, **_kwargs) -> None:
        pass

    def note_extra_call(self) -> None:
        pass


class _OkResponse:
    status_code = 200
    text = "{}"

    def raise_for_status(self) -> None:
        return None


class _ResetSession:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        raise requests.ConnectionError("Connection reset by peer")


class _NodeAwareSession:
    """Resets for exits in ``bad``; answers 200 for every other exit."""

    def __init__(self, bad: set[str]) -> None:
        self.bad = bad
        self.exits: list[str] = []

    def request(self, method, url, proxies=None, **kwargs):
        exit_url = str((proxies or {}).get("https", ""))
        self.exits.append(exit_url)
        if exit_url in self.bad:
            # zh-CN Windows wording; the classifier must not depend on the text.
            raise requests.ConnectionError(
                "Connection aborted.", ConnectionResetError(10054, "远端主机强迫关闭了一个现有的连接。")
            )
        return _OkResponse()


class _AlternatingSession:
    """Resets on every odd attempt, answers on every even one."""

    def __init__(self) -> None:
        self.n = 0
        self.exits: list[str] = []

    def request(self, method, url, proxies=None, **kwargs):
        self.n += 1
        self.exits.append(str((proxies or {}).get("https", "")))
        if self.n % 2 == 1:
            raise requests.ConnectionError("Connection reset by peer")
        return _OkResponse()


@pytest.fixture(autouse=True)
def clean_guard():
    http_mod.reset_endpoint_guard()
    with base_mod._LAST_GOOD_LOCK:
        base_mod._LAST_GOOD.clear()
    yield
    http_mod.reset_endpoint_guard()


def _offline(monkeypatch, session, proxy_for=None):
    monkeypatch.setattr(http_mod, "_session_for", lambda source: session)
    monkeypatch.setattr(http_mod, "limiter_for", lambda source: _NoLimit())
    if proxy_for is None:
        monkeypatch.setattr(http_mod, "_proxy_for", lambda source, exclude=frozenset(): (None, None))
    else:
        monkeypatch.setattr(http_mod, "_proxy_for", proxy_for)


def _pool_of(*nodes: str) -> tuple[ProxyPool, list[ProxyNode]]:
    proxies = [ProxyNode(raw=raw) for raw in nodes]
    return ProxyPool(proxies), proxies


def _offline_pool(monkeypatch, session, pool: ProxyPool) -> None:
    def proxy_for(source, exclude=frozenset()):
        node = pool.acquire(exclude)
        return node.as_requests_proxy(), node

    monkeypatch.setattr(http_mod, "endpoint_limiter", lambda family: _NoLimit())
    monkeypatch.setattr("tickhub_cn.proxy.pool.get_pool", lambda: pool)
    _offline(monkeypatch, session, proxy_for=proxy_for)


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


def test_limiter_reports_the_wait_instead_of_sleeping_past_the_cap():
    limiter = http_mod.RateLimiter(min_interval=25.0, max_calls=3, window=60)
    limiter.acquire()
    started = time.monotonic()
    with pytest.raises(http_mod.RateLimitPaced) as paced:
        limiter.acquire(max_wait=http_mod.ENDPOINT_MAX_WAIT)
    assert time.monotonic() - started < 0.5
    assert paced.value.retry_after > http_mod.ENDPOINT_MAX_WAIT


def test_paced_endpoint_fails_fast_so_the_tool_can_fall_back(monkeypatch):
    session = _ResetSession()
    _offline(monkeypatch, session)
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    http_mod.endpoint_limiter(http_mod.endpoint_family(url)).acquire()

    started = time.monotonic()
    with pytest.raises(TickHubError) as err:
        http_mod.fetch("eastmoney", "GET", url, retries=0)
    assert time.monotonic() - started < 1.0
    assert err.value.code == "QFSD-7001"
    assert err.value.detail["reason"] == "endpoint_paced"
    assert err.value.detail["retry_after_s"] >= 20
    assert session.calls == 0
    assert http_mod.ENDPOINT_BREAKER.open_families() == {}


def test_reset_without_another_exit_trips_immediately(monkeypatch):
    """No spare healthy node means no evidence, so the old fail-fast stands."""
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


def test_one_reset_fails_over_to_a_healthy_node_without_tripping(monkeypatch):
    pool, (bad, good) = _pool_of("http://127.0.0.1:9", "http://127.0.0.1:10")
    session = _NodeAwareSession({bad.requests_url()})
    _offline_pool(monkeypatch, session, pool)

    url = "https://push2.eastmoney.com/api/qt/stock/get"
    resp = http_mod.fetch("eastmoney", "GET", url, retries=2)

    assert resp.status_code == 200
    assert session.exits == [bad.requests_url(), good.requests_url()]
    assert http_mod.ENDPOINT_BREAKER.open_families() == {}
    assert bad.failures == 1  # a reset implicates the exit that carried it


def test_two_resets_from_two_exits_trip_for_600s(monkeypatch):
    pool, (first, second) = _pool_of("http://127.0.0.1:9", "http://127.0.0.1:10")
    session = _NodeAwareSession({first.requests_url(), second.requests_url()})
    _offline_pool(monkeypatch, session, pool)

    with pytest.raises(TickHubError) as err:
        http_mod.fetch("eastmoney", "GET", "https://push2.eastmoney.com/api/qt/stock/get", retries=2)

    assert err.value.code == "QFSD-7001"
    assert err.value.detail["retry_after_s"] == 600
    assert err.value.detail["error"] == "ConnectionResetError"
    assert session.exits == [first.requests_url(), second.requests_url()]
    assert http_mod.ENDPOINT_BREAKER.open_families() != {}


def test_three_family_resets_in_one_window_trip_even_with_a_spare_exit(monkeypatch):
    """A retry that keeps resetting is still a ban: 3 resets/60s closes the family."""
    pool, _nodes = _pool_of("http://127.0.0.1:9", "http://127.0.0.1:10")
    session = _AlternatingSession()
    _offline_pool(monkeypatch, session, pool)
    url = "https://push2.eastmoney.com/api/qt/stock/get"

    http_mod.fetch("eastmoney", "GET", url, retries=2)
    http_mod.fetch("eastmoney", "GET", url, retries=2)

    with pytest.raises(TickHubError) as err:
        http_mod.fetch("eastmoney", "GET", url, retries=2)

    assert err.value.code == "QFSD-7001"
    assert err.value.detail["retry_after_s"] == 600
    assert http_mod.ENDPOINT_BREAKER.open_families() != {}
    assert len(session.exits) == 5  # third call trips before spending the failover


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ConnectionResetError(10054, "远端主机强迫关闭了一个现有的连接。"), True),
        (ConnectionAbortedError(10053, "你的主机中的软件中止了一个已建立的连接。"), True),
        (requests.ConnectionError("Connection aborted.", ConnectionResetError(10054, "远端。")), True),
        (requests.ConnectionError("Connection reset by peer"), True),
        (requests.HTTPError("500 Server Error"), False),
        (requests.Timeout("read timed out"), False),
    ],
)
def test_localized_resets_are_classified_by_type_and_errno(exc, expected):
    assert http_mod._is_connection_reset(exc) is expected


def test_boxed_reset_keeps_the_socket_verdict_for_the_public_helper(monkeypatch):
    class _LocalizedReset:
        def request(self, *args, **kwargs):
            raise requests.ConnectionError(
                "Connection aborted.", ConnectionResetError(10054, "远端主机强迫关闭了一个现有的连接。")
            )

    _offline(monkeypatch, _LocalizedReset())

    with pytest.raises(TickHubError) as err:
        http_mod.fetch("eastmoney", "GET", "https://push2.eastmoney.com/api/qt/stock/get", retries=0)

    assert err.value.detail["error"] == "ConnectionResetError"
    assert err.value.detail["errno"] == 10054
    assert http_mod.is_connection_reset(err.value) is True


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
