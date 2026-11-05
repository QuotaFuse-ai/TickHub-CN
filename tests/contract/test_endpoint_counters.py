"""QUOT-370: read-only per-endpoint-family attempt counters.

The counters must answer "same 150 codes, different nodes: did the reset land
only on the bad node?" -- so they count every real round trip (retries
included), say which proxy exit each reset came through, and are readable from
the existing health file without changing retry or breaker behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from tickhub_cn.core import http as http_mod
from tickhub_cn.core.errors import TickHubError
from tickhub_cn.proxy.pool import ProxyNode, ProxyPool

ULIST = "https://push2.eastmoney.com/api/qt/ulist.np/get"
FAMILY = "push2.eastmoney.com/api/qt/ulist/get"


class _NoLimit:
    def acquire(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def note_extra_call(self) -> None:
        pass


class _Response:
    status_code = 200
    text = "{}"

    def raise_for_status(self) -> None:
        return None


class _OkSession:
    def request(self, *args: Any, **kwargs: Any) -> _Response:
        return _Response()


class _NodeSession:
    """Resets when the request goes through ``bad``; answers on anything else."""

    def __init__(self, bad: str) -> None:
        self.bad = bad
        self.exits: list[str] = []

    def request(self, method: str, url: str, proxies: dict[str, str] | None = None, **kwargs: Any) -> _Response:
        host = str((proxies or {}).get("https", ""))
        self.exits.append(host)
        if host == self.bad:
            raise requests.ConnectionError("Connection reset by peer")
        return _Response()


class _ResetSession:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, *args: Any, **kwargs: Any) -> _Response:
        self.calls += 1
        raise requests.ConnectionError("Connection reset by peer")


@pytest.fixture(autouse=True)
def clean_guard():
    http_mod.reset_endpoint_guard()
    yield
    http_mod.reset_endpoint_guard()


def _offline(monkeypatch: pytest.MonkeyPatch, session: Any, proxy_for: Any = None) -> None:
    monkeypatch.setattr(http_mod, "_session_for", lambda source: session)
    monkeypatch.setattr(http_mod, "limiter_for", lambda source: _NoLimit())
    monkeypatch.setattr(http_mod, "endpoint_limiter", lambda family: _NoLimit())
    if proxy_for is None:
        monkeypatch.setattr(http_mod, "_proxy_for", lambda source, exclude=frozenset(): (None, None))
    else:
        monkeypatch.setattr(http_mod, "_proxy_for", proxy_for)


def _counters() -> dict[str, Any]:
    return http_mod.ENDPOINT_COUNTERS.snapshot()


# --------------------------------------------------------------------- counting


def test_one_round_trip_is_one_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch, _OkSession())

    http_mod.fetch("eastmoney", "GET", ULIST)

    row = _counters()[FAMILY]
    assert (row["attempts"], row["successes"], row["failures"], row["resets"]) == (1, 1, 0, 0)


def test_non_push2_requests_are_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch, _OkSession())

    http_mod.fetch("eastmoney", "GET", "https://datacenter-web.eastmoney.com/api/data/v1/get")

    assert _counters() == {}


def test_a_transport_retry_is_counted_as_a_second_real_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FlakySession:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, *args: Any, **kwargs: Any) -> _Response:
            self.calls += 1
            if self.calls == 1:
                raise requests.ConnectTimeout("timed out")
            return _Response()

    session = _FlakySession()
    _offline(monkeypatch, session)

    http_mod.fetch("eastmoney", "GET", ULIST, retries=1)

    row = _counters()[FAMILY]
    assert (row["attempts"], row["successes"], row["failures"], row["resets"]) == (2, 1, 1, 0)


# --------------------------------------------------------------- node attribution


def test_reset_records_which_proxy_node_carried_it(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = "http://127.0.0.1:9"
    session = _NodeSession(bad)
    node = ProxyNode(raw=bad)
    _offline(monkeypatch, session, proxy_for=lambda source, exclude=frozenset(): (node.as_requests_proxy(), node))

    with pytest.raises(TickHubError) as err:
        http_mod.fetch("eastmoney", "GET", ULIST)

    assert err.value.code == "QFSD-7001"
    row = _counters()[FAMILY]
    assert row["attempts"] == 1 and row["resets"] == 1
    assert row["last_reset_node"] == node.id
    assert row["reset_nodes"] == [{"node": node.id, "at": row["last_reset_at"]}]
    assert session.exits == [bad]


def test_a_direct_request_records_no_node(monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch, _ResetSession())

    with pytest.raises(TickHubError):
        http_mod.fetch("eastmoney", "GET", ULIST)

    row = _counters()[FAMILY]
    assert row["resets"] == 1
    assert row["last_reset_node"] is None


def test_resets_are_attributed_to_the_node_that_answered_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """The evidence shape QUOT-370 asks for: one batch, reset only on the bad exit."""
    bad = ProxyNode(raw="http://127.0.0.1:9")
    good = ProxyNode(raw="http://127.0.0.1:10")
    pool = ProxyPool([bad, good])
    monkeypatch.setattr("tickhub_cn.proxy.pool.get_pool", lambda: pool)

    def proxy_for(source: str, exclude: set[str] | frozenset[str] = frozenset()) -> Any:
        node = pool.acquire(exclude)
        return node.as_requests_proxy(), node

    session = _NodeSession(bad.requests_url())
    _offline(monkeypatch, session, proxy_for=proxy_for)

    resp = http_mod.fetch("eastmoney", "GET", ULIST, retries=2)
    assert resp.status_code == 200  # answered by the good exit, no per-code degradation

    row = _counters()[FAMILY]
    assert (row["attempts"], row["successes"], row["failures"], row["resets"]) == (2, 1, 1, 1)
    assert row["last_reset_node"] == bad.id
    assert session.exits == [bad.requests_url(), good.requests_url()]
    # The per-node breakdown is the evidence a disabled node stopped carrying
    # traffic: one attempt each, the only reset on the bad exit.
    assert row["nodes"][bad.id] == {
        "node": bad.id,
        "attempts": 1,
        "successes": 0,
        "failures": 1,
        "resets": 1,
        "last_reset_at": row["last_reset_at"],
    }
    assert row["nodes"][good.id] == {
        "node": good.id,
        "attempts": 1,
        "successes": 1,
        "failures": 0,
        "resets": 0,
        "last_reset_at": None,
    }


def test_a_disabled_node_keeps_its_counts_and_stops_accumulating(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabling an exit must freeze its tally while the other one takes the load."""
    first = ProxyNode(raw="http://127.0.0.1:11")
    second = ProxyNode(raw="http://127.0.0.1:12")
    pool = ProxyPool([first, second])
    monkeypatch.setattr("tickhub_cn.proxy.pool.get_pool", lambda: pool)

    def proxy_for(source: str, exclude: set[str] | frozenset[str] = frozenset()) -> Any:
        node = pool.acquire(exclude)
        return node.as_requests_proxy(), node

    _offline(monkeypatch, _OkSession(), proxy_for=proxy_for)

    http_mod.fetch("eastmoney", "GET", ULIST)
    used, spare = (first, second) if _counters()[FAMILY]["nodes"].get(first.id, {}).get("attempts") else (second, first)
    before = dict(_counters()[FAMILY]["nodes"][used.id])

    used.enabled = False
    http_mod.fetch("eastmoney", "GET", ULIST)
    row = _counters()[FAMILY]
    assert row["nodes"][used.id] == before
    assert row["nodes"][spare.id]["attempts"] == 1
    assert row["attempts"] == 2


# ---------------------------------------------------------------- health channel


def test_counters_land_in_the_health_file_beside_source_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "source_health.json"
    registry = http_mod.HealthRegistry(state)
    monkeypatch.setattr(http_mod, "HEALTH", registry)
    _offline(monkeypatch, _ResetSession())

    with pytest.raises(TickHubError):
        http_mod.fetch("eastmoney", "GET", ULIST)
    # ``record`` flushed before the reset was counted, so force a fresh write.
    registry._dirty = True
    registry._flushed = 0.0
    registry.flush()

    raw = json.loads(state.read_text(encoding="utf-8"))
    assert raw["sources"]["eastmoney"]["failures"] == 1
    assert raw["endpoints"][FAMILY]["resets"] == 1
    assert http_mod.read_health_state(state) == raw["sources"]
    assert http_mod.read_endpoint_counters(state)[FAMILY]["attempts"] == 1


def test_stale_endpoint_counters_are_ignored(tmp_path: Path) -> None:
    state = tmp_path / "old.json"
    state.write_text(
        json.dumps({"updated_at": "2020-01-01T00:00:00", "sources": {}, "endpoints": {FAMILY: {"attempts": 3}}}),
        encoding="utf-8",
    )
    assert http_mod.read_endpoint_counters(state) == {}


def test_guard_reset_clears_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch, _OkSession())
    http_mod.fetch("eastmoney", "GET", ULIST)
    assert _counters()

    http_mod.reset_endpoint_guard()

    assert _counters() == {}
