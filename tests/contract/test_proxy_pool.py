"""Proxy pool: failure accounting from ``fetch`` and the interval health checker."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
import requests

from tickhub_cn.core import http as http_mod
from tickhub_cn.core.errors import TickHubError
from tickhub_cn.proxy.pool import (
    ProxyNode,
    ProxyPool,
    add_proxy_node,
    load_entries,
    masked_entry,
    remove_proxy_node,
    set_proxy_node_enabled,
)


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


def test_disabled_node_is_never_acquired(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        proxy_pool_config_path=str(tmp_path / "proxy_pool.json"),
        proxy_pool_enable=True,
    )
    monkeypatch.setattr("tickhub_cn.proxy.pool.get_settings", lambda: settings)
    add_proxy_node({"url": "http://127.0.0.1:1"}, settings)
    second = add_proxy_node({"url": "http://127.0.0.1:2"}, settings)
    set_proxy_node_enabled(second["key"], False, settings)

    pool = ProxyPool.from_settings()
    assert [n.enabled for n in pool.nodes] == [True, False]

    nodes = [ProxyNode(raw="http://127.0.0.1:1"), ProxyNode(raw="http://127.0.0.1:2", enabled=False)]
    pool = ProxyPool(nodes)
    assert pool.acquire() is nodes[0]
    assert pool.acquire() is nodes[0], "a disabled node must never be handed out"


def test_add_toggle_remove_round_trip_preserves_password(tmp_path):
    settings = SimpleNamespace(proxy_pool_config_path=str(tmp_path / "nested" / "proxy_pool.json"))
    added = add_proxy_node(
        {"url": "10.0.0.1:8080", "username": "alice-long", "password": "p@ss word"},
        settings,
    )
    assert added["url"] == "http://10.0.0.1:8080"
    assert added["username"] == "al***"
    assert "p@ss word" not in json.dumps(added)

    stored = json.loads((tmp_path / "nested" / "proxy_pool.json").read_text(encoding="utf-8"))
    assert stored["proxies"][0]["password"] == "p@ss word"
    assert stored["proxies"][0]["username"] == "alice-long"

    with pytest.raises(ValueError, match="already exists"):
        add_proxy_node({"url": "http://10.0.0.1:8080", "username": "alice-long"}, settings)

    toggled = set_proxy_node_enabled(added["key"], False, settings)
    assert toggled is not None and toggled["enabled"] is False
    entries = load_entries(settings)
    assert entries[0]["password"] == "p@ss word", "toggling must not blank the stored password"

    removed = remove_proxy_node(added["key"], settings)
    assert removed is not None and removed["key"] == added["key"]
    assert load_entries(settings) == []
    assert remove_proxy_node(added["key"], settings) is None


def test_masked_entry_hides_inline_credentials():
    row = masked_entry("http://alice:secret@10.0.0.1:8080")
    assert row["url"] == "http://10.0.0.1:8080"
    assert row["username"] == ""
    assert "alice" not in json.dumps(row)
    assert "secret" not in json.dumps(row)


def test_invalid_proxy_url_is_rejected(tmp_path):
    settings = SimpleNamespace(proxy_pool_config_path=str(tmp_path / "proxy_pool.json"))
    for bad in ("", "   ", "ftp://10.0.0.1:21", "http://10.0.0.1:not-a-port"):
        with pytest.raises(ValueError):
            add_proxy_node({"url": bad}, settings)
