"""QUOT-407 限速探针的离线用例：reset 判定、阶梯计划、结论口径。

探针本身不发请求，这些用例也不碰网络——它们守住的是「中文 Windows 上 reset
必须被判出来」和「安全档 → 上线间隔」两条最容易写错的口径。
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ulist_rate_probe", ROOT / "scripts" / "ulist_rate_probe.py")
probe = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = probe
_spec.loader.exec_module(probe)


def _stage(interval: float, attempts: int, kind: str = "ok", rows: int = 150, planned: int | None = None) -> probe.Stage:
    stage = probe.Stage(interval=interval, planned=planned or attempts, seconds=attempts * interval)
    stage.samples = [probe.Sample(index=i, at="2026-11-05T22:00:00+08:00", kind=kind, rt_ms=100.0, rows=rows) for i in range(attempts)]
    return stage


def test_reset_by_type_ignores_localized_message():
    exc = ConnectionResetError(10054, "远端主机强迫关闭了一个现有的连接。")
    assert probe.is_reset(exc) is True


def test_reset_unwrapped_from_urlerror_reason():
    inner = ConnectionResetError(10054, "远端主机强迫关闭了一个现有的连接。")
    wrapped = urllib.error.URLError(inner)
    assert probe.is_reset(wrapped) is True
    assert probe._classify(wrapped) == "reset"


def test_winerror_without_matching_errno_still_counts():
    exc = OSError("connection aborted")
    exc.winerror = 10053
    assert probe.is_reset(exc) is True


def test_timeout_and_http_errors_are_not_resets():
    assert probe.is_reset(TimeoutError("timed out")) is False
    assert probe.is_reset(urllib.error.HTTPError("u", 429, "Too Many", {}, None)) is False
    assert probe._classify(urllib.error.HTTPError("u", 429, "Too Many", {}, None)) == "blocked"
    assert probe._classify(urllib.error.HTTPError("u", 503, "Unavailable", {}, None)) == "server_error"


def test_stage_plan_honours_both_minimums():
    assert probe.plan_stage(50.0, 600, 60) == (60, 3000.0)
    assert probe.plan_stage(5.0, 600, 60) == (120, 600.0)
    assert probe.plan_stage(1.0, 600, 60) == (600, 600.0)


def test_stage_plan_follows_authorised_tier_conversion():
    assert probe.plan_stage(15.0, 600, 0) == (40, 600.0)
    assert probe.plan_stage(30.0, 600, 0) == (20, 600.0)
    assert probe.plan_stage(60.0, 600, 0) == (10, 600.0)
    baseline = [stage.summary() for stage in (_stage(15.0, 40), _stage(30.0, 20), _stage(60.0, 10))]
    assert probe.verdict(baseline)["safe_interval_s"] == 15.0


def test_verdict_in_census_mode_makes_no_rate_claim():
    stage = _stage(15.0, 40, kind="reset", rows=0)
    result = probe.verdict([stage.summary()], mode="census")
    assert result["safe_interval_s"] is None
    assert "census" in result["note"]


def test_abort_on_first_reset():
    stage = _stage(10.0, 1, kind="reset", rows=0)
    assert probe._abort_reason(stage, 1, 0, baseline_p95=100.0) == "连接被重置"


def test_five_second_stage_supports_second_level():
    result = probe.verdict([stage.summary() for stage in (_stage(50.0, 60), _stage(20.0, 60), _stage(10.0, 60), _stage(5.0, 120))])
    assert result["safe_interval_s"] == 5.0
    assert result["recommended_interval_s"] == 10.0
    assert result["supports_second_level"] is True


def test_ten_second_stage_stays_minute_level():
    result = probe.verdict([stage.summary() for stage in (_stage(50.0, 60), _stage(20.0, 60), _stage(10.0, 60))])
    assert result["safe_interval_s"] == 10.0
    assert result["recommended_interval_s"] == 20.0
    assert result["supports_second_level"] is False


def test_incomplete_stage_is_not_safe():
    stage = _stage(5.0, 40, planned=120)
    result = probe.verdict([stage.summary()])
    assert result["safe_interval_s"] is None
