"""Proxy pool: failure accounting from ``fetch`` and the interval health checker."""

from __future__ import annotations

import threading
import time

import pytest
import requests

from tickhub_cn.core import http as http_mod
from tickhub_cn.core.errors import TickHubError
from tickhub_cn.proxy.pool import ProxyNode, ProxyPool


def _pool(*urls: str) -> ProxyPool:
    return ProxyPool([ProxyNode(raw=url) for url in urls])


class _RaisingSession:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def request(self, *args, **kwargs):
        raise self.exc


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class _StatusSession:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def request(self, *args, **kwargs):
        return _Response(self.status_code)


class _NoLimit:
    def acquire(self) -> None:
        pass


@pytest.fixture
def node_and_pool(monkeypatch):
    pool = _pool("http://127.0.0.1:9")
    node = pool.nodes[0]
    monkeypatch.setattr(http_mod, "_proxy_for", lambda source, exclude=frozenset(): (node.as_requests_proxy(), node))
    monkeypatch.setattr(http_mod, "limiter_for", lambda source: _NoLimit())
    monkeypatch.setattr("tickhub_cn.proxy.pool.get_pool", lambda: pool)
    return node, pool


def test_mark_disables_only_after_three_consecutive_failures():
    pool = _pool("http://127.0.0.1:9")
    node = pool.nodes[0]
    pool.mark(node, False, 12.0)
    pool.mark(node, False, 12.0)
    assert node.healthy and node.failures == 2
    assert pool.acquire() is node

    pool.mark(node, False, 12.0)
    assert not node.healthy and node.failures == 3
    assert pool.acquire() is None


def test_success_resets_the_consecutive_counter_and_revives():
    pool = _pool("http://127.0.0.1:9")
    node = pool.nodes[0]
    for _ in range(3):
        pool.mark(node, False)
    assert not node.healthy

    pool.mark(node, True, 7.0)
    assert node.healthy and node.failures == 0 and node.latency_ms == 7.0


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ProxyError("tunnel refused"),
        requests.exceptions.ConnectTimeout("handshake timed out"),
        requests.exceptions.SSLError("certificate verify failed"),
    ],
)
def test_connection_faults_count_against_the_node(monkeypatch, node_and_pool, exc):
    node, _ = node_and_pool
    monkeypatch.setattr(http_mod, "_session_for", lambda source: _RaisingSession(exc))

    with pytest.raises(TickHubError):
        http_mod.fetch("testproxy", "GET", "http://example.invalid/x", retries=0)

    assert node.failures == 1


@pytest.mark.parametrize("status", [404, 500])
def test_http_statuses_are_not_proxy_faults(monkeypatch, node_and_pool, status):
    node, _ = node_and_pool
    monkeypatch.setattr(http_mod, "_session_for", lambda source: _StatusSession(status))

    with pytest.raises(TickHubError):
        http_mod.fetch("testproxy", "GET", "http://example.invalid/x", retries=0)

    assert node.failures == 0
    assert node.healthy


def test_acquire_skips_excluded_nodes_but_reuses_one_when_all_are_tried():
    pool = _pool("http://127.0.0.1:1", "http://127.0.0.1:2")
    first, second = pool.nodes

    picked = pool.acquire(exclude={first.id})
    assert picked is second

    picked = pool.acquire(exclude={first.id, second.id})
    assert picked in (first, second), "an excluded pool must not silently go direct"


def test_retry_skips_the_node_that_just_failed(monkeypatch):
    pool = _pool("http://127.0.0.1:1", "http://127.0.0.1:2")
    used: list[dict[str, str]] = []

    class _FlakySession:
        def request(self, *args, **kwargs):
            used.append(kwargs["proxies"])
            if len(used) == 1:
                raise requests.exceptions.ProxyError("tunnel refused")
            return _Response(200)

    monkeypatch.setattr(http_mod, "limiter_for", lambda source: _NoLimit())
    monkeypatch.setattr(http_mod, "_session_for", lambda source: _FlakySession())
    monkeypatch.setattr(http_mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr("tickhub_cn.proxy.pool.get_pool", lambda: pool)

    resp = http_mod.fetch("testproxy", "GET", "http://example.invalid/x", retries=1)

    assert resp.status_code == 200
    assert used[0] != used[1], "retry must not reuse the exit that just failed"
    assert pool.nodes[0].failures == 1
    assert pool.nodes[1].failures == 0


def test_monitor_probes_every_interval_and_stops():
    pool = _pool("http://127.0.0.1:1", "http://127.0.0.1:2")
    calls: list[ProxyNode] = []
    first_round = threading.Event()

    def fake_check(node, timeout=None):
        calls.append(node)
        if len(calls) >= len(pool.nodes):
            first_round.set()
        return True

    pool.check = fake_check  # type: ignore[method-assign]

    assert pool.start_monitor(0.05) is True
    assert pool.start_monitor(0.05) is False, "second start must not spawn another thread"
    assert first_round.wait(2.0), "monitor never probed the pool"

    pool.stop_monitor()
    settled = len(calls)
    time.sleep(0.15)
    assert len(calls) == settled, "monitor still probing after stop"


def test_monitor_is_not_started_without_nodes():
    assert ProxyPool([]).start_monitor(0.05) is False
