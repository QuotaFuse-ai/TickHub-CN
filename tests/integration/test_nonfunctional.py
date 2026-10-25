"""NF-01..NF-06 (docs/TEST_PLAN.md): import cost, latency, logs, cache bounds."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _python(code: str, *, cwd: Path, timeout: float = 30.0, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=timeout,
        check=False,
    )


def test_nf01_cold_import_is_fast_and_offline(tmp_path: Path) -> None:
    script = """
import socket, time

def _blocked(*_args, **_kwargs):
    raise AssertionError("network access during import")

socket.socket.connect = _blocked
socket.create_connection = _blocked

started = time.perf_counter()
import tickhub_cn
print(round((time.perf_counter() - started) * 1000, 1))
print(tickhub_cn.__version__)
"""
    proc = _python(script, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    elapsed_ms, version = proc.stdout.split()
    assert version == "0.1.1"
    assert float(elapsed_ms) < 300.0, f"cold import took {elapsed_ms} ms"


def test_nf02_cached_cli_call_is_fast(run_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import quotes

    payload = {"data": {"sh600000": {"qfqday": [["2026-10-20", "10", "10.1", "10.2", "9.9", "1000", "10000"]]}}}
    monkeypatch.setattr(quotes, "request", lambda *_a, **_k: payload)

    first_code, first_out, _ = run_cli("kline", "get", "600000", "--limit", "5")
    assert first_code == 0
    assert json.loads(first_out)["meta"]["cache_hit"] is False

    started = time.perf_counter()
    second_code, second_out, _ = run_cli("kline", "get", "600000", "--limit", "5")
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert second_code == 0
    assert json.loads(second_out)["meta"]["cache_hit"] is True
    assert elapsed_ms < 150.0, f"cached CLI call took {elapsed_ms:.1f} ms"


def test_nf03_doctor_completes_with_an_unreachable_source(
    run_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tickhub_cn.core.http import HEALTH

    monkeypatch.setattr(HEALTH, "_state", dict(HEALTH._state))
    HEALTH.record("tencent", False, 1200.0, "connect timeout")

    started = time.perf_counter()
    code, out, _ = run_cli("doctor")
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert code == 0
    payload = json.loads(out)
    sources = {row["source"]: row for row in payload["data"]["sources"]}
    assert sources["tencent"]["ok"] is False
    assert sources["tencent"]["last_error"] == "connect timeout"
    assert elapsed_ms < 10_000.0, f"doctor took {elapsed_ms:.0f} ms"


def test_nf05_memory_cache_evicts_expired_keys() -> None:
    from tickhub_cn.cache.redis_cache import get_cache

    cache = get_cache()
    assert cache.stats()["backend"] == "memory"

    for index in range(10_000):
        cache.memory.set(f"qf:test:cache:bulk:{index}", index, 0.01)
    time.sleep(0.1)

    # Any inspection sweeps expired entries, so the store cannot grow forever.
    stats = cache.stats()
    assert stats["keys"] == 0
    assert len(cache.memory.data) == 0


def test_nf06_every_stderr_log_line_is_json(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(ROOT / "src"),
        PYTHONIOENCODING="utf-8",
        LOG_LEVEL="info",
        LOG_SINK="stderr",
        LOG_FILE=str(tmp_path / "logs" / "tickhub.log"),
        REDIS_URL="redis://127.0.0.1:6399/0",
        ARCHIVE_DIR=str(tmp_path / "var" / "archive"),
        ARCHIVE_DB_PATH=str(tmp_path / "var" / "archive" / "tickhub.db"),
        FIELD_MAP_PATH=str(tmp_path / "field_map.toml"),
    )
    (tmp_path / "logs").mkdir(exist_ok=True)
    shutil.copy(ROOT / "field_map.toml", tmp_path / "field_map.toml")
    proc = subprocess.run(
        [sys.executable, "-m", "tickhub_cn", "doctor"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    assert lines, "expected at least the redis-unavailable warning"
    for line in lines:
        row = json.loads(line)
        assert {"ts", "level", "logger", "message"} <= set(row), line
