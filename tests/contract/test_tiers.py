"""CT-06: output-granularity presets (``brief`` / ``medium`` / ``full``).

Precedence under test: per-call ``fields`` > per-call ``tier`` > env override
> ``field_map.toml`` ``default_fields`` > full payload.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import call_tool
from tickhub_cn.fieldmap.loader import FieldMap, reload_field_map
from tickhub_cn.fieldmap.tiers import BRIEF_FIELDS, MEDIUM_FIELDS, normalize_tier

TOML = """
[tools.apply_adjust]
default_fields = ["adjusted"]

[tiers.brief]
apply_adjust = ["code"]

[tiers.medium]
"*" = ["code", "adjusted", "factor"]
"""

PARAMS = {"code": "600000", "closes": "10", "factors": "1.1"}


def _write_map(tmp_path: Path, text: str) -> None:
    (tmp_path / "field_map.toml").write_text(text, encoding="utf-8")
    reload_field_map()


def test_normalize_tier() -> None:
    assert normalize_tier(" Brief ") == "brief"
    assert normalize_tier(None) == ""
    with pytest.raises(ValueError):
        normalize_tier("tiny")


def test_brief_uses_tool_override(tmp_path: Path) -> None:
    _write_map(tmp_path, TOML)
    payload = call_tool("apply_adjust", {**PARAMS, "tier": "brief"})
    assert payload["data"] == {"code": "600000"}


def test_medium_falls_back_to_star_override(tmp_path: Path) -> None:
    _write_map(tmp_path, TOML)
    payload = call_tool("apply_adjust", {**PARAMS, "tier": "medium"})
    assert set(payload["data"]) == {"code", "adjusted"}


def test_builtin_preset_when_file_has_no_tier_section(tmp_path: Path) -> None:
    _write_map(tmp_path, "[tools.apply_adjust]\ndefault_fields = [\"adjusted\"]\n")
    payload = call_tool("apply_adjust", {**PARAMS, "tier": "brief"})
    # built-in brief knows ``code`` but not ``adjusted``; the bounded fallback
    # keeps the row from coming back empty.
    assert payload["data"]["code"] == "600000"


def test_per_call_fields_beat_tier(tmp_path: Path) -> None:
    _write_map(tmp_path, TOML)
    payload = call_tool("apply_adjust", {**PARAMS, "tier": "brief", "fields": "adjusted"})
    assert payload["data"] == {"adjusted": [11.0]}


def test_tier_beats_env_and_default_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_map(tmp_path, TOML)
    monkeypatch.setenv("TICKHUB_FIELDS_APPLY_ADJUST", "adjusted")
    assert call_tool("apply_adjust", PARAMS)["data"] == {"adjusted": [11.0]}
    assert call_tool("apply_adjust", {**PARAMS, "tier": "brief"})["data"] == {"code": "600000"}


def test_full_lifts_default_projection(tmp_path: Path) -> None:
    _write_map(tmp_path, TOML)
    payload = call_tool("apply_adjust", {**PARAMS, "tier": "full"})
    assert set(payload["data"]) == {"code", "adjusted"}


def test_unknown_tier_is_invalid_argument(tmp_path: Path) -> None:
    _write_map(tmp_path, TOML)
    with pytest.raises(TickHubError) as info:
        call_tool("apply_adjust", {**PARAMS, "tier": "tiny"})
    assert info.value.code == "QFSD-9001"
    assert "tier" in info.value.message


def test_preset_that_matches_nothing_keeps_bounded_slice() -> None:
    field_map = FieldMap(tools={}, raw={"tiers": {"brief": {"*": ["no_such_field"]}}})
    row = {f"k{i}": i for i in range(20)}
    out = field_map.apply("demo", row, tier="brief")
    assert len(out) == 8
    assert list(out) == [f"k{i}" for i in range(8)]


def test_medium_fallback_caps_at_24() -> None:
    field_map = FieldMap(tools={}, raw={"tiers": {"medium": {"*": ["no_such_field"]}}})
    out = field_map.apply("demo", {f"k{i}": i for i in range(50)}, tier="medium")
    assert len(out) == 24


def test_tier_projects_nested_rows() -> None:
    rows = [{"date": "2026-10-20", "open": 1, "close": 2, "turnover_rate": 3} for _ in range(3)]
    field_map = FieldMap(tools={}, raw={})
    out = field_map.apply("demo", {"code": "600000", "rows": rows}, tier="brief")
    assert out["code"] == "600000"
    assert all(set(r) == {"date", "close"} for r in out["rows"])


def test_tier_hides_still_apply() -> None:
    field_map = FieldMap(
        tools={},
        raw={"defaults": {"hide": ["secret"]}, "tiers": {"full": {}}},
    )
    out = field_map.apply("demo", {"code": "600000", "secret": "x"}, tier="full")
    assert out == {"code": "600000"}


def test_presets_are_supersets() -> None:
    assert set(BRIEF_FIELDS) < set(MEDIUM_FIELDS)
