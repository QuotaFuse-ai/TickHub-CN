"""CT-01: the published catalogue and the live registry must not drift.

The catalogue in docs/MCP_API_CONTRACT.md section 9 is the contract other teams
code against, so it is parsed here literally rather than restated in Python.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

import tickhub_cn.datasources  # noqa: F401  (registers every tool)
from tickhub_cn.datasources.base import ALIASES, LAYERS, REGISTRY, tool_catalog
from tickhub_cn.mcp.server import tool_count

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "docs" / "MCP_API_CONTRACT.md"

ALLOWED_TTLS = {60, 86400, 604800, None}
EXPECTED_LAYERS = {
    "quotes",
    "financials",
    "market",
    "funds",
    "macro",
    "announcements",
    "index",
    "futures",
    "event",
    "research",
    "sentiment",
    "option",
    "bond",
    "valuation",
}
_SECTION_RE = re.compile(r"^###\s+(\w+)\s+\((\d+)\)\s*$")
_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|(.*)\|\s*$")


@dataclass(frozen=True)
class CatalogueRow:
    name: str
    params: tuple[str, ...]
    ttl: int | None
    description: str


def _split_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def parse_catalogue(path: Path = CATALOGUE) -> dict[str, dict[str, CatalogueRow]]:
    """Return ``{"layers": {layer: {tool: row}}, "alias": {tool: row}}``."""
    text = path.read_text(encoding="utf-8")
    start = text.index("## 9. Layer and tool catalogue")
    end = text.find("\n## ", start + 5)
    section = text[start : end if end != -1 else len(text)]

    result: dict[str, dict[str, CatalogueRow]] = {"layers": {}}
    current: dict[str, CatalogueRow] | None = None
    for raw in section.splitlines():
        match = _SECTION_RE.match(raw.strip())
        if match:
            name = match.group(1)
            target = result.setdefault("alias", {}) if name == "alias" else result["layers"].setdefault(name, {})
            assert len(target) == 0, f"duplicate section {name}"
            current = target
            continue
        row = _ROW_RE.match(raw.strip())
        if not row or current is None:
            continue
        name = row.group(1)
        cells = [c.strip() for c in row.group(2).split("|")]
        # cells: params, ttl, description (description may contain a pipe)
        if len(cells) < 3:
            continue
        params_cell, ttl_cell = cells[0], cells[1]
        description = "|".join(cells[2:]).strip()
        params = () if params_cell == "(alias)" else tuple(p.strip() for p in params_cell.split(",") if p.strip())
        ttl = None if ttl_cell == "-" else int(ttl_cell)
        current[name] = CatalogueRow(name=name, params=params, ttl=ttl, description=description)
    return result


def test_catalogue_section_counts() -> None:
    parsed = parse_catalogue()
    layers = parsed["layers"]
    assert set(layers) == EXPECTED_LAYERS
    assert {name: len(rows) for name, rows in layers.items()} == {
        name: len(tools) for name, tools in LAYERS.items()
    }
    assert sum(len(rows) for rows in layers.values()) == 124
    assert len(parsed["alias"]) == 25


def test_registry_matches_catalogue_exactly() -> None:
    parsed = parse_catalogue()
    for layer, rows in parsed["layers"].items():
        for name, row in rows.items():
            assert name in REGISTRY, f"{name} documented in {layer} but not registered"
            spec = REGISTRY[name]
            assert spec.layer == layer, f"{name}: layer {spec.layer} != {layer}"
            assert tuple(spec.params) == row.params, f"{name}: params {spec.params} != {row.params}"
            assert spec.ttl == row.ttl, f"{name}: ttl {spec.ttl} != {row.ttl}"
            assert spec.description == row.description, f"{name}: description drifted"

    documented = {name for rows in parsed["layers"].values() for name in rows}
    assert documented == set(REGISTRY), "registered tools missing from the catalogue"


def test_layer_membership_is_consistent() -> None:
    seen: dict[str, str] = {}
    for layer, tools in LAYERS.items():
        assert layer in EXPECTED_LAYERS
        for name in tools:
            assert name in REGISTRY, f"{name} listed in LAYERS but missing from REGISTRY"
            assert name not in seen, f"{name} appears in both {seen.get(name)} and {layer}"
            seen[name] = layer
    assert set(seen) == set(REGISTRY)
    assert set(LAYERS) == EXPECTED_LAYERS


def test_every_tool_declares_a_supported_ttl_and_description() -> None:
    for name, spec in REGISTRY.items():
        assert spec.description, f"{name} has no description"
        assert spec.layer in EXPECTED_LAYERS, f"{name} has unknown layer {spec.layer}"
        assert spec.ttl in ALLOWED_TTLS, f"{name} has ttl {spec.ttl}, allowed {sorted(map(str, ALLOWED_TTLS))}"


def test_aliases_match_catalogue() -> None:
    parsed = parse_catalogue()["alias"]
    assert set(parsed) == set(ALIASES)
    for name, row in parsed.items():
        target = ALIASES[name]
        assert target in REGISTRY, f"{name} -> {target} is not registered"
        assert row.description == f"compatibility alias of {target}"
        assert row.ttl is None
        assert row.params == ()


def test_tool_catalog_matches_registry() -> None:
    catalog = tool_catalog()
    assert len(catalog) == len(REGISTRY)
    by_name = {row["name"]: row for row in catalog}
    for name, spec in REGISTRY.items():
        row = by_name[name]
        assert row["layer"] == spec.layer
        assert row["ttl_seconds"] == spec.ttl
        assert row["description"] == spec.description
        assert tuple(row["params"]) == tuple(spec.params)


def test_mcp_tool_count_includes_aliases() -> None:
    assert len(REGISTRY) == 124
    assert len(ALIASES) == 25
    assert tool_count() == 149


def test_tool_names_quoted_in_docs_are_callable() -> None:
    """A copy-paste example that names a non-existent tool fails with QFSD-9002."""
    names: set[str] = set()
    for page in (ROOT / "docs").glob("*.md"):
        text = page.read_text(encoding="utf-8")
        names.update(re.findall(r'call_tool\(\s*"([^"]+)"', text))
        names.update(re.findall(r"th-cn tool call (\S+)", text))
    assert names
    for name in sorted(names):
        target = ALIASES.get(name, name)
        assert target in REGISTRY, f"{name} appears in docs but is not a tool"


@pytest.mark.parametrize("name", ["stock_profile", "tencent_ticks", "apply_adjust"])
def test_ttl_regressions(name: str) -> None:
    """Guards the two TTL reconciliations recorded in docs/decisions/."""
    expected = {"stock_profile": 604800, "tencent_ticks": 60, "apply_adjust": None}
    assert REGISTRY[name].ttl == expected[name]
