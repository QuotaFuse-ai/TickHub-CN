"""CT-07: cache controls — hit/miss, ``_refresh``, ``_no_cache``, key format, Redis-down mode."""

from __future__ import annotations

import json

import pytest

from tickhub_cn.cache.redis_cache import TTL, get_cache, params_hash
from tickhub_cn.datasources import quotes
from tickhub_cn.datasources.base import REGISTRY
from tests.support import kline_payload, qt_line


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_request(source: str, method: str, url: str, *, params=None, **kwargs):
        calls.append(source)
        return qt_line(price=10.0 + len(calls))

    monkeypatch.setattr(quotes, "request", fake_request)
    return calls


def test_second_call_is_a_cache_hit(upstream: list[str]) -> None:
    first = quotes.tencent_quote("600000")
    second = quotes.tencent_quote("600000")

    assert first["meta"]["cache_hit"] is False
    assert second["meta"]["cache_hit"] is True
    assert first["data"][0]["price"] == second["data"][0]["price"] == 11.0
    assert upstream == ["tencent"]


def test_refresh_bypasses_cache_and_replaces_entry(upstream: list[str]) -> None:
    quotes.tencent_quote("600000")
    refreshed = quotes.tencent_quote("600000", _refresh=True)
    again = quotes.tencent_quote("600000")

    assert refreshed["meta"]["cache_hit"] is False
    assert refreshed["data"][0]["price"] == 12.0
    assert again["meta"]["cache_hit"] is True
    assert again["data"][0]["price"] == 12.0
    assert len(upstream) == 2


def test_no_cache_neither_reads_nor_writes(upstream: list[str]) -> None:
    cached = quotes.tencent_quote("600000")
    uncached = quotes.tencent_quote("600000", _no_cache=True)
    again = quotes.tencent_quote("600000")

    assert uncached["meta"]["cache_hit"] is False
    assert uncached["data"][0]["price"] == 12.0
    assert again["meta"]["cache_hit"] is True
    assert again["data"][0]["price"] == cached["data"][0]["price"] == 11.0
    assert len(upstream) == 2


def test_cache_key_matches_documented_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quotes, "request", lambda *a, **k: kline_payload())
    quotes.tencent_kline("600000")
    keys = [k for k in get_cache().memory.data if ":cache:" in k]

    assert len(keys) == 1
    prefix, _, digest = keys[0].rpartition(":")
    assert prefix == "qf:test:cache:kline:600000:tencent_kline"
    assert digest == params_hash(
        {"code": "600000", "period": "daily", "adjust": "qfq", "limit": 320, "start": "", "end": ""}
    )


def test_ttl_tiers_match_the_contract() -> None:
    assert TTL == {"static": 604800, "semi_static": 86400, "realtime": 60, "health": 120}
    assert REGISTRY["tencent_quote"].ttl == TTL["realtime"]
    assert REGISTRY["tencent_kline"].ttl == TTL["semi_static"]
    assert REGISTRY["stock_profile"].ttl == TTL["static"]
    assert REGISTRY["apply_adjust"].ttl is None


def test_redis_down_degrades_to_memory(upstream: list[str]) -> None:
    cache = get_cache()
    assert cache.redis_ok is False
    assert cache.stats()["backend"] == "memory"

    quotes.tencent_quote("600000")
    assert quotes.tencent_quote("600000")["meta"]["cache_hit"] is True
    assert len(upstream) == 1


def test_flush_clears_matching_domain(upstream: list[str]) -> None:
    quotes.tencent_quote("600000")
    removed = get_cache().flush("quote")
    after = quotes.tencent_quote("600000")

    assert removed == 1
    assert after["meta"]["cache_hit"] is False
    assert len(upstream) == 2


def test_stats_account_for_hits_and_misses(upstream: list[str]) -> None:
    quotes.tencent_quote("600000")
    quotes.tencent_quote("600000")
    stats = get_cache().stats()

    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 0.5
    assert stats["env"] == "test"
