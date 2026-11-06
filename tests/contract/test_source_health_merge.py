"""QUOT-468: ``th-cn source health`` must reflect the peer's real counters.

The command runs in a short-lived CLI process whose own ``HEALTH`` registry is
empty, so its only honest sources are the shared cache and the file the peer
mirrors its counters to. Before this fix it read neither the file nor (in
practice) Redis, and always printed all-green zeros -- making the intraday
acceptance criterion unreadable.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest


def _point_state_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Set the state path and rebuild settings (they are lru_cached)."""
    from tickhub_cn.core.config import reload_settings

    monkeypatch.setenv("HEALTH_STATE_PATH", str(path))
    reload_settings()


def _write_state(path: Path, sources: dict[str, dict[str, Any]]) -> None:
    path.write_text(
        json.dumps(
            {
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "sources": sources,
                "endpoints": {},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def isolated_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Redis and an empty in-process registry.

    The CLI must work off the state file alone, and the global ``HEALTH``
    singleton can carry records leaked by other tests, which would otherwise
    show up in ``observed``.
    """
    from tickhub_cn.cache import redis_cache
    from tickhub_cn.core import http as http_mod

    class _Empty:
        def read_health(self) -> dict[str, Any]:
            return {}

    monkeypatch.setattr(redis_cache, "get_cache", lambda: _Empty())
    monkeypatch.setattr(http_mod, "HEALTH", http_mod.HealthRegistry())


def test_health_reads_peer_counts_from_the_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli_json, isolated_health: None
) -> None:
    state = tmp_path / "source_health.json"
    _write_state(
        state,
        {
            "eastmoney": {
                "source": "eastmoney",
                "ok": False,
                "successes": 41,
                "failures": 2,
                "last_latency_ms": 88.0,
                "last_checked_at": "2026-11-06T09:30:05+0800",
                "last_error": "HTTP 500",
            }
        },
    )
    _point_state_at(monkeypatch, state)

    code, payload, _ = run_cli_json("source", "health")
    assert code == 0
    rows = {r["source"]: r for r in payload["data"]["sources"]}
    em = rows["eastmoney"]
    assert em["successes"] == 41
    assert em["failures"] == 2
    assert em["ok"] is False
    assert em["last_error"] == "HTTP 500"
    # ``observed`` counts sources with a real observation, not just registered ones.
    assert payload["meta"]["observed"] == 1


def test_health_observed_is_zero_without_any_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli_json, isolated_health: None
) -> None:
    _point_state_at(monkeypatch, tmp_path / "missing.json")
    code, payload, _ = run_cli_json("source", "health")
    assert code == 0
    assert payload["meta"]["observed"] == 0
    assert all(r["successes"] == 0 and r["failures"] == 0 for r in payload["data"]["sources"])


def test_health_ignores_a_stale_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli_json, isolated_health: None
) -> None:
    state = tmp_path / "stale.json"
    state.write_text(
        json.dumps(
            {
                "updated_at": "2020-01-01T00:00:00",
                "sources": {"eastmoney": {"source": "eastmoney", "failures": 9}},
                "endpoints": {},
            }
        ),
        encoding="utf-8",
    )
    _point_state_at(monkeypatch, state)
    code, payload, _ = run_cli_json("source", "health")
    assert code == 0
    rows = {r["source"]: r for r in payload["data"]["sources"]}
    assert rows["eastmoney"]["failures"] == 0
    assert payload["meta"]["observed"] == 0


def test_health_cached_counts_win_by_recency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_cli_json
) -> None:
    """A fresher Redis observation beats an older file, and vice versa."""
    from tickhub_cn.cache import redis_cache

    state = tmp_path / "source_health.json"
    _write_state(
        state,
        {"tencent": {"source": "tencent", "successes": 1, "last_checked_at": "2026-11-06T09:00:00+0800"}},
    )
    _point_state_at(monkeypatch, state)

    class _Fresh:
        def read_health(self) -> dict[str, Any]:
            return {
                "tencent": {
                    "source": "tencent",
                    "successes": 7,
                    "last_checked_at": "2026-11-06T09:31:00+0800",
                }
            }

    monkeypatch.setattr(redis_cache, "get_cache", lambda: _Fresh())
    from tickhub_cn.core import http as http_mod

    monkeypatch.setattr(http_mod, "HEALTH", http_mod.HealthRegistry())
    code, payload, _ = run_cli_json("source", "health")
    assert code == 0
    rows = {r["source"]: r for r in payload["data"]["sources"]}
    assert rows["tencent"]["successes"] == 7


def test_record_mirrors_to_the_shared_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The peer's ``HEALTH.record`` populates the Redis health channel."""
    from tickhub_cn.cache import redis_cache
    from tickhub_cn.core import http as http_mod

    captured: dict[str, Any] = {}

    class _Sink:
        def record_health(self, source: str, payload: dict[str, Any]) -> None:
            captured[source] = payload

    monkeypatch.setattr(redis_cache, "get_cache", lambda: _Sink())
    registry = http_mod.HealthRegistry()
    registry.record("sina", True, 12.3)
    assert captured["sina"]["successes"] == 1
    assert captured["sina"]["ok"] is True

    registry.record("sina", False, 30.0, "boom")
    assert captured["sina"]["failures"] == 1
    assert captured["sina"]["last_error"] == "boom"
