"""External field mapping (aliases / hiding / default projection).

Priority: per-call ``fields`` > environment override > ``field_map.toml`` >
original full payload.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import get_settings
from ..logging.setup import get_logger

try:
    import toml
except Exception:  # pragma: no cover
    toml = None


@dataclass
class ToolMapping:
    alias: dict[str, str] = field(default_factory=dict)
    hide: list[str] = field(default_factory=list)
    default_fields: list[str] = field(default_factory=list)
    template: str | None = None


class FieldMap:
    def __init__(self, tools: dict[str, ToolMapping] | None = None, raw: dict[str, Any] | None = None, error: str | None = None):
        self.tools = tools or {}
        self.raw = raw or {}
        self.error = error
        self._global_hide = list((self.raw.get("defaults") or {}).get("hide", []))

    @classmethod
    def load(cls, path: str | Path | None = None) -> "FieldMap":
        settings = get_settings()
        path = Path(path or settings.field_map_path)
        if not path.exists():
            return cls()
        if toml is None:
            return cls(error="toml library missing")
        try:
            raw = toml.loads(path.read_text(encoding="utf-8"))
            tools: dict[str, ToolMapping] = {}
            for name, conf in (raw.get("tools") or {}).items():
                if not isinstance(conf, dict):
                    raise ValueError(f"tool {name} must be a table")
                alias = conf.get("alias", {}) or {}
                hide = conf.get("hide", []) or []
                default_fields = conf.get("default_fields", []) or []
                if not isinstance(alias, dict) or not isinstance(hide, list) or not isinstance(default_fields, list):
                    raise ValueError(f"invalid mapping types for tool {name}")
                tools[name] = ToolMapping(alias={str(k): str(v) for k, v in alias.items()}, hide=[str(x) for x in hide], default_fields=[str(x) for x in default_fields], template=conf.get("template"))
            return cls(tools=tools, raw=raw)
        except Exception as exc:
            get_logger("fieldmap").warning(
                "field map invalid, falling back to full fields",
                extra={"error": str(exc), "error_code": "QFSD-3001"},
            )
            return cls(error=str(exc))

    def mapping_for(self, tool: str) -> ToolMapping:
        return self.tools.get(tool, ToolMapping())

    def _select_fields(self, tool: str, env_key: str | None) -> list[str] | None:
        env_name = f"TICKHUB_FIELDS_{tool.upper()}" if env_key is None else env_key
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            return [x.strip() for x in env_value.split(",") if x.strip()]
        default = self.mapping_for(tool).default_fields
        return default or None

    def apply(
        self,
        tool: str,
        data: Any,
        *,
        fields: list[str] | str | None = None,
        env_key: str | None = None,
    ) -> Any:
        mapping = self.mapping_for(tool)
        selected: list[str] | None
        if isinstance(fields, str):
            selected = [x.strip() for x in fields.split(",") if x.strip()]
        elif fields:
            selected = list(fields)
        else:
            selected = self._select_fields(tool, env_key)

        def _rename_and_hide(row: dict[str, Any]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for key, value in row.items():
                if key in mapping.hide or key in self._global_hide:
                    continue
                mapped = mapping.alias.get(key, key)
                if selected and mapped not in selected and key not in selected and not mapped.startswith("_"):
                    continue
                out[mapped] = value
            return out

        if isinstance(data, list):
            return [_rename_and_hide(r) if isinstance(r, dict) else r for r in data]
        if isinstance(data, dict):
            if mapping.hide or mapping.alias or selected:
                nested: dict[str, Any] = {}
                scalars: dict[str, Any] = {}
                for key, value in data.items():
                    if isinstance(value, list) and value and isinstance(value[0], dict):
                        rows = self.apply(tool, value, fields=selected)
                        # A selection names row fields (``revenue``, ``date``),
                        # so the list container survives even though it is not
                        # itself named. When nothing matches the rows, the
                        # container is dropped unless it was selected directly.
                        if selected is None or any(rows) or key in selected:
                            nested[key] = rows
                    else:
                        scalars[key] = value
                projected = _rename_and_hide(scalars)
                for key, value in nested.items():
                    if key in mapping.hide or key in self._global_hide:
                        continue
                    projected[mapping.alias.get(key, key)] = value
                return projected
            return data
        return data


_MAP: FieldMap | None = None


def get_field_map() -> FieldMap:
    global _MAP
    if _MAP is None:
        _MAP = FieldMap.load()
    return _MAP


def reload_field_map() -> FieldMap:
    global _MAP
    _MAP = FieldMap.load()
    return _MAP
