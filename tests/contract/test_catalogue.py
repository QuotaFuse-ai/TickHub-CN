"""CT-01: the published catalogue and the live registry must not drift.

The published catalogue is the README tool tables (the only human-facing doc
the repository ships). It is parsed here literally rather than restated in
Python, so a tool that is renamed, re-layered or dropped without updating the
README fails the suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import tickhub_cn.datasources  # noqa: F401  (registers every tool)
from tickhub_cn.datasources.base import ALIASES, LAYERS, REGISTRY, tool_catalog
from tickhub_cn.mcp.server import tool_count

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"

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
# One README heading covers three sibling layers.
_SECTION_LAYERS = {"option": {"option", "bond", "valuation"}}

_ANCHOR_RE = re.compile(r'^<a id="layer-(\w+)"></a>$')
_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|(.*)\|\s*$")
_ALIAS_PAIR_RE = re.compile(r"`([^`]+)`\s*\|\s*`([^`]+)`")


def _parse_params(cell: str) -> tuple[str, ...]:
    if cell.strip() in ("-", "—", ""):
        return ()
    return tuple(p.strip().strip("`") for p in cell.split(",") if p.strip())


def parse_catalogue(path: Path = README) -> dict[str, dict]:
    """Return ``{"layers": {layer: {tool: params}}, "alias": {alias: target}}``."""
    text = path.read_text(encoding="utf-8")
    start = text.index("## 数据覆盖")
    end = text.index("\n## ", start + 5)
    section = text[start : end if end != -1 else len(text)]

    layers: dict[str, dict[str, tuple[str, ...]]] = {}
    aliases: dict[str, str] = {}
    current: str | None = None
    for raw in section.splitlines():
        line = raw.strip()
        anchor = _ANCHOR_RE.match(line)
        if anchor:
            current = anchor.group(1)
            layers.setdefault(current, {})
            continue
        row = _ROW_RE.match(line)
        if not row or current is None:
            continue
        if current == "aliases":
            for alias, target in _ALIAS_PAIR_RE.findall(line):
                aliases[alias] = target
            continue
        layers[current][row.group(1)] = _parse_params(row.group(2).split("|")[0])
    return {"layers": layers, "alias": aliases}


def test_catalogue_section_counts() -> None:
    parsed = parse_catalogue()
    layers = {name: rows for name, rows in parsed["layers"].items() if name != "aliases"}
    assert set(layers) == set(_SECTION_LAYERS) | (EXPECTED_LAYERS - _SECTION_LAYERS["option"])
    assert sum(len(rows) for rows in layers.values()) == 124
    assert len(parsed["alias"]) == 25


def test_registry_matches_catalogue_exactly() -> None:
    parsed = parse_catalogue()
    documented: set[str] = set()
    for section, rows in parsed["layers"].items():
        if section == "aliases":
            continue
        allowed = _SECTION_LAYERS.get(section, {section})
        for name, params in rows.items():
            assert name in REGISTRY, f"{name} documented in {section} but not registered"
            spec = REGISTRY[name]
            assert spec.layer in allowed, f"{name}: layer {spec.layer} not under {section}"
            assert tuple(spec.params) == params, f"{name}: params {spec.params} != {params}"
            documented.add(name)
    assert documented == set(REGISTRY), "registered tools missing from the README catalogue"


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
    for name, target in parsed.items():
        assert target == ALIASES[name], f"{name} -> {target} != {ALIASES[name]}"
        assert target in REGISTRY, f"{name} -> {target} is not registered"


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
    pages = [README, *sorted((ROOT / "skills").rglob("*.md"))]
    names: set[str] = set()
    for page in pages:
        text = page.read_text(encoding="utf-8")
        names.update(re.findall(r'call_tool\(\s*"([^"]+)"', text))
        names.update(re.findall(r"th-cn tool call (\S+)", text))
    assert names
    for name in sorted(names):
        target = ALIASES.get(name, name)
        assert target in REGISTRY, f"{name} appears in docs but is not a tool"


@pytest.mark.parametrize("name", ["stock_profile", "tencent_ticks", "apply_adjust"])
def test_ttl_regressions(name: str) -> None:
    """Guards the two TTL reconciliations that must not silently regress."""
    expected = {"stock_profile": 604800, "tencent_ticks": 60, "apply_adjust": None}
    assert REGISTRY[name].ttl == expected[name]
