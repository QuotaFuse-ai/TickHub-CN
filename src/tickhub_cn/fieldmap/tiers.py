"""Output-granularity presets: ``brief`` / ``medium`` / ``full``.

A tier is a projection preset. ``brief`` keeps identity and price-snapshot
fields, ``medium`` adds OHLC, valuation and context fields, and ``full``
lifts the field restriction (aliases and hides from ``field_map.toml`` still
apply). A per-call ``fields=`` list always wins over ``tier``.

Presets live in ``field_map.toml`` under ``[tiers.brief]`` / ``[tiers.medium]``
keyed by tool name, with ``"*"`` as the default for every tool. The built-in
lists below are the fallback when the file carries no override.
"""

from __future__ import annotations

from typing import Any

TIER_NAMES = ("brief", "medium", "full")

BRIEF_FIELDS = [
    "code",
    "name",
    "date",
    "time",
    "trade_date",
    "report_date",
    "price",
    "close",
    "change",
    "pct_chg",
    "volume",
    "amount",
    "title",
]

MEDIUM_FIELDS = BRIEF_FIELDS + [
    "open",
    "high",
    "low",
    "pre_close",
    "turnover_rate",
    "pe",
    "pb",
    "total_mv",
    "circ_mv",
    "industry",
    "publish_time",
    "announcement_date",
    "report_name",
    "url",
    "rating",
    "target_price",
]

DEFAULT_TIERS: dict[str, list[str]] = {
    "brief": BRIEF_FIELDS,
    "medium": MEDIUM_FIELDS,
}

# Row cap used only when a tier's curated list matches nothing in a payload
# (an upstream with entirely different field names must not come back blank).
TIER_FALLBACK_ROWS = {"brief": 8, "medium": 24}


def normalize_tier(value: Any) -> str:
    tier = str(value or "").strip().lower()
    if not tier:
        return ""
    if tier not in TIER_NAMES:
        raise ValueError(f"unsupported tier: {value} (expected brief / medium / full)")
    return tier


def tier_fields(raw: dict[str, Any], tier: str, tool: str) -> list[str] | None:
    """Resolve one tier for one tool, honouring ``[tiers.<tier>]`` overrides."""
    table = (raw.get("tiers") or {}).get(tier) or {}
    value = table.get(tool)
    if value is None:
        value = table.get("*")
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return list(DEFAULT_TIERS.get(tier, [])) or None


__all__ = ["DEFAULT_TIERS", "TIER_FALLBACK_ROWS", "TIER_NAMES", "normalize_tier", "tier_fields"]
