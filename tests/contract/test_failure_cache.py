"""QUOT-370: failed and partial answers must never be served from the cache.

A cached failure hands the next retry the same broken answer for the whole TTL,
so the retry never reaches the upstream and a breaker opened on it only counts
cache hits.
"""

from __future__ import annotations

from typing import Any

import pytest

from tickhub_cn.cache.redis_cache import reset_cache
from tickhub_cn.datasources import base as base_mod

FAILED = {
    "ok": False,
    "data": {},
    "meta": {"source": "fake", "fetched_at": "", "cache_hit": False, "confidence": "B",
             "trace_id": "t", "error_code": "QFSD-7001", "retryable": True},
}
PARTIAL = {
    "ok": False,
    "data": {"flows": {"600001": {"code": "600001"}}},
    "meta": {"source": "fake", "fetched_at": "", "cache_hit": False, "confidence": "B",
             "trace_id": "t", "error_code": "QFSD-8001", "partial": True, "retryable": True},
}
GOOD = {
    "ok": True,
    "data": {"value": 1},
    "meta": {"source": "fake", "fetched_at": "", "cache_hit": False, "confidence": "B", "trace_id": "t"},
}


@pytest.fixture
def probe_tool():
    reset_cache()
    with base_mod._LAST_GOOD_LOCK:
        base_mod._LAST_GOOD.clear()

    def _make(name: str, payloads: dict[str, dict[str, Any]], **tool_kwargs: Any):
        calls: dict[str, int] = {}

        @base_mod.tool(name=name, layer="_probe", ttl=60, cache_domain="_probe", **tool_kwargs)
        def _probe(code: str = "X") -> dict:
            """Probe tool."""
            calls[code] = calls.get(code, 0) + 1
            return payloads[code]

        return _probe, calls

    made: list[str] = []
    yield _make, made

    for name in made:
        base_mod.REGISTRY.pop(name, None)
        base_mod.LAYERS.get("_probe", []).remove(name)
    base_mod.LAYERS.pop("_probe", None)
    reset_cache()
    with base_mod._LAST_GOOD_LOCK:
        base_mod._LAST_GOOD.clear()


def test_a_failed_answer_is_asked_again_on_the_next_call(probe_tool) -> None:
    make, made = probe_tool
    fn, calls = make("_probe_failed", {"BAD": FAILED})
    made.append("_probe_failed")

    first = fn(code="BAD")
    second = fn(code="BAD")

    assert calls["BAD"] == 2
    assert first["meta"]["cache_hit"] is False
    assert second["meta"]["cache_hit"] is False
    assert second["ok"] is False


def test_a_partial_answer_is_asked_again_without_a_retry_inside_the_call(probe_tool) -> None:
    make, made = probe_tool
    fn, calls = make("_probe_partial", {"HALF": PARTIAL})
    made.append("_probe_partial")

    first = fn(code="HALF")
    second = fn(code="HALF")

    assert calls["HALF"] == 2
    assert first["data"]["flows"] == second["data"]["flows"] == {"600001": {"code": "600001"}}
    assert first["meta"]["partial"] is True and second["meta"]["partial"] is True


def test_a_successful_answer_is_still_served_from_the_cache(probe_tool) -> None:
    make, made = probe_tool
    fn, calls = make("_probe_good", {"OK": GOOD})
    made.append("_probe_good")

    first = fn(code="OK")
    second = fn(code="OK")

    assert calls["OK"] == 1
    assert first["meta"]["cache_hit"] is False
    assert second["meta"]["cache_hit"] is True


def test_recovery_after_a_failure_is_visible_on_the_next_call(probe_tool) -> None:
    reset_cache()
    answers = {"n": 0}

    @base_mod.tool(name="_probe_recover", layer="_probe", ttl=60, cache_domain="_probe")
    def _probe(code: str = "R") -> dict:
        """Probe tool."""
        answers["n"] += 1
        return FAILED if answers["n"] == 1 else GOOD

    try:
        assert _probe(code="R")["ok"] is False
        healed = _probe(code="R")
    finally:
        base_mod.REGISTRY.pop("_probe_recover", None)
        base_mod.LAYERS["_probe"].remove("_probe_recover")

    assert answers["n"] == 2
    assert healed["ok"] is True and healed["data"] == {"value": 1}
    assert healed["meta"]["cache_hit"] is False


def test_opt_in_keeps_caching_failures_for_callers_that_want_it(probe_tool) -> None:
    make, made = probe_tool
    fn, calls = make("_probe_legacy", {"OLD": FAILED}, cache_failures=True)
    made.append("_probe_legacy")

    fn(code="OLD")
    second = fn(code="OLD")

    assert calls["OLD"] == 1
    assert second["meta"]["cache_hit"] is True


def test_a_failed_answer_does_not_become_the_stale_fallback(probe_tool) -> None:
    make, made = probe_tool
    fn, _calls = make("_probe_stale", {"S": FAILED})
    made.append("_probe_stale")

    fn(code="S")

    with base_mod._LAST_GOOD_LOCK:
        assert base_mod._LAST_GOOD == {}
