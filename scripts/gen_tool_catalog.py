"""Regenerate the tool catalogue used by docs and the skill package.

Usage:
    python scripts/gen_tool_catalog.py --format skill --out skills/tickhub-cn/references/tools.md
    python scripts/gen_tool_catalog.py --format json

The catalogue is derived from the live registry, so it cannot drift from the
code the way a hand-written table does. Layer order follows the reading order in
the README; tools inside a layer are alphabetical.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

LAYER_ORDER = [
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
]

LAYER_TITLES = {
    "quotes": "Quotes & K-lines",
    "financials": "Financials",
    "market": "Market breadth & pools",
    "funds": "Funds & flows",
    "macro": "Macro & rates",
    "announcements": "Announcements & news",
    "index": "Indices & calendar",
    "futures": "Futures & commodities",
    "event": "Event driven",
    "research": "Research",
    "sentiment": "Sentiment & IR",
    "option": "Options",
    "bond": "Convertible bonds",
    "valuation": "Valuation",
}


def load_catalog() -> list[dict]:
    src_root = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(src_root))
    import tickhub_cn.datasources  # noqa: F401  (registers every tool)
    from tickhub_cn.datasources.base import ALIASES, REGISTRY

    rows = [
        {
            "name": spec.name,
            "layer": spec.layer,
            "description": spec.description,
            "ttl_seconds": spec.ttl,
            "params": list(spec.params),
        }
        for spec in REGISTRY.values()
    ]
    rows.extend(
        {
            "name": alias,
            "layer": "alias",
            "description": f"compatibility alias of {target}",
            "ttl_seconds": None,
            "params": list(REGISTRY[target].params),
        }
        for alias, target in ALIASES.items()
    )
    return rows


def _table_row(tool: dict) -> str:
    params = ", ".join(f"`{p}`" for p in tool["params"]) or "—"
    ttl = "—" if tool["ttl_seconds"] is None else str(tool["ttl_seconds"])
    desc = (tool["description"] or "").replace("|", "\\|").strip()
    return f"| `{tool['name']}` | {params} | {ttl} | {desc} |"


def render_skill(rows: list[dict]) -> str:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["layer"]].append(row)

    canonical = [r for r in rows if r["layer"] != "alias"]
    aliases = grouped.get("alias", [])
    out = [
        "# Tool catalogue",
        "",
        f"{len(canonical)} canonical tools + {len(aliases)} aliases = "
        f"{len(rows)} MCP names. Generated with `scripts/gen_tool_catalog.py`; "
        "do not edit by hand.",
        "",
        "TTL is the cache lifetime in seconds; `—` means the alias shares its "
        "target's behaviour and the response is not cached separately.",
        "",
    ]
    for layer in LAYER_ORDER:
        if layer not in grouped:
            continue
        out += [
            f"## {LAYER_TITLES.get(layer, layer)} (`{layer}`)",
            "",
            "| Tool | Parameters | TTL (s) | Description |",
            "| --- | --- | --- | --- |",
        ]
        out += [_table_row(t) for t in sorted(grouped[layer], key=lambda r: r["name"])]
        out.append("")

    out += [
        "## Compatibility aliases (`alias`)",
        "",
        "Aliases mirror the names used by the original a-stock-data client. "
        "They take the same parameters as their target.",
        "",
        "| Alias | Target | Parameters | Description |",
        "| --- | --- | --- | --- |",
    ]
    for alias in sorted(aliases, key=lambda r: r["name"]):
        target = alias["description"].replace("compatibility alias of ", "")
        params = ", ".join(f"`{p}`" for p in alias["params"]) or "—"
        out.append(f"| `{alias['name']}` | `{target}` | {params} | alias |")
    out.append("")
    return "\n".join(out)


def render_json(rows: list[dict]) -> str:
    return json.dumps({"count": len(rows), "tools": rows}, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--format",
        choices=("skill", "json"),
        default="skill",
        help="skill = markdown catalogue, json = machine-readable registry dump",
    )
    parser.add_argument("--out", help="write to this path instead of stdout")
    args = parser.parse_args()

    rows = load_catalog()
    text = render_skill(rows) if args.format == "skill" else render_json(rows)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(rows)} names)", file=sys.stderr)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
