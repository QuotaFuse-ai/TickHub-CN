"""CT-05: field projection precedence, aliases, hiding and the invalid-TOML fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from tickhub_cn.datasources import call_tool
from tickhub_cn.fieldmap.loader import FieldMap, get_field_map, reload_field_map

TOML = """
[defaults]
hide = ["secret"]

[tools.apply_adjust]
default_fields = ["code", "adjusted", "_note"]
alias = { adjusted = "adj" }
hide = ["hidden"]
"""

BAD_TOML = """
[tools.apply_adjust
default_fields = ["code"
"""


def _write_map(tmp_path: Path, text: str) -> None:
    (tmp_path / "field_map.toml").write_text(text, encoding="utf-8")
    reload_field_map()


def test_per_call_fields_win_over_defaults(tmp_path: Path) -> None:
    _write_map(tmp_path, TOML)
    payload = call_tool("apply_adjust", {"code": "600000", "closes": "10", "factors": "1.1"})
    assert payload["data"] == {"code": "600000", "adj": [11.0]}

    projected = call_tool("apply_adjust", {"code": "600000", "closes": "10", "factors": "1.1", "fields": "code"})
    assert projected["data"] == {"code": "600000"}


def test_env_override_beats_toml_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_map(tmp_path, TOML)
    monkeypatch.setenv("TICKHUB_FIELDS_APPLY_ADJUST", "adj")
    payload = call_tool("apply_adjust", {"code": "600000", "closes": "10", "factors": "1.1"})
    assert payload["data"] == {"adj": [11.0]}


def test_per_call_fields_beat_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_map(tmp_path, TOML)
    monkeypatch.setenv("TICKHUB_FIELDS_APPLY_ADJUST", "adj")
    payload = call_tool("apply_adjust", {"code": "600000", "closes": "10", "factors": "1.1", "fields": "code,adj"})
    assert set(payload["data"]) == {"code", "adj"}


def test_alias_hide_and_underscore_keys() -> None:
    field_map = FieldMap(
        tools={"demo": _mapping(alias={"a": "alpha"}, hide=["b"], default_fields=["alpha"])},
    )
    out = field_map.apply("demo", {"a": 1, "b": 2, "_trace": 3, "c": 4})
    assert out == {"alpha": 1, "_trace": 3}

    out_full = field_map.apply("demo", {"a": 1, "b": 2, "_trace": 3, "c": 4}, fields="a,c")
    # selection accepts the source spelling as well as the mapped one
    assert out_full == {"alpha": 1, "_trace": 3, "c": 4}

    out_mapped = field_map.apply("demo", {"a": 1, "b": 2, "_trace": 3, "c": 4}, fields="alpha")
    assert out_mapped == {"alpha": 1, "_trace": 3}


def _mapping(**kwargs):
    from tickhub_cn.fieldmap.loader import ToolMapping

    return ToolMapping(**kwargs)


def test_meta_survives_projection(tmp_path: Path) -> None:
    _write_map(tmp_path, TOML)
    payload = call_tool("apply_adjust", {"code": "600000", "closes": "10", "factors": "1.1", "fields": "code"})
    assert set(payload["meta"]) >= {"source", "fetched_at", "cache_hit", "confidence", "trace_id"}
    assert payload["ok"] is True


def test_malformed_map_falls_back_to_full_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[tuple[tuple, dict]] = []

    class _Capture:
        def warning(self, *args, **kwargs):
            warnings.append((args, kwargs))

    monkeypatch.setattr("tickhub_cn.fieldmap.loader.get_logger", lambda name: _Capture())
    _write_map(tmp_path, BAD_TOML)

    field_map = get_field_map()
    assert field_map.error
    assert warnings and warnings[0][1]["extra"]["error_code"] == "QFSD-3001"

    payload = call_tool("apply_adjust", {"code": "600000", "closes": "10", "factors": "1.1"})
    assert payload["data"] == {"code": "600000", "adjusted": [11.0]}


def test_missing_map_is_not_an_error(tmp_path: Path) -> None:
    (tmp_path / "field_map.toml").unlink()
    reload_field_map()
    assert get_field_map().error is None
    payload = call_tool("apply_adjust", {"code": "600000", "closes": "10", "factors": "1.1"})
    assert payload["data"] == {"code": "600000", "adjusted": [11.0]}
