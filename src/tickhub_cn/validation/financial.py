"""Financial statement cross-check (勾稽校验)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Rule:
    name: str
    formula: str
    left: str
    right: tuple[str, ...]
    tolerance: float = 0.01
    severity: str = "fail"


DEFAULT_RULES = [
    Rule("balance_sheet", "assets = liabilities + equity", "assets", ("liabilities", "equity")),
    Rule("net_profit", "net_profit = total_profit - income_tax", "net_profit", ("total_profit", "-income_tax"), severity="warn"),
    Rule("cash_flow", "ending_cash = beginning_cash + net_cash_increase", "ending_cash", ("beginning_cash", "net_cash_increase")),
]


def _num(value: Any) -> float | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _resolve(values: dict[str, Any], field: str) -> float | None:
    sign = 1.0
    name = field
    if field.startswith("-"):
        sign, name = -1.0, field[1:]
    raw = values.get(name)
    if isinstance(raw, dict):
        raw = raw.get("value")
    value = _num(raw)
    return None if value is None else sign * value


def crosscheck(
    values: dict[str, Any],
    *,
    tolerance: float = 0.01,
    strict: bool = True,
    rules: list[Rule] | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for rule in rules or DEFAULT_RULES:
        left = _resolve(values, rule.left)
        parts = [_resolve(values, f) for f in rule.right]
        if left is None or any(p is None for p in parts):
            results.append(
                {
                    "rule": rule.name,
                    "formula": rule.formula,
                    "status": "skip",
                    "severity": rule.severity,
                    "diff": None,
                    "reason": "missing fields",
                }
            )
            continue
        right = sum(parts)
        diff = left - right
        rel = abs(diff) / max(abs(left), abs(right), 1.0)
        status = "pass" if rel <= tolerance else ("warn" if rule.severity == "warn" and not strict else "fail")
        results.append(
            {
                "rule": rule.name,
                "formula": rule.formula,
                "status": status,
                "severity": rule.severity,
                "left": round(left, 4),
                "right": round(right, 4),
                "diff": round(diff, 4),
                "relative": round(rel, 6),
            }
        )
    statuses = {r["status"] for r in results}
    overall = "fail" if "fail" in statuses else ("warn" if "warn" in statuses else ("pass" if "pass" in statuses else "skip"))
    return {"status": overall, "tolerance": tolerance, "rules": results}


def normalize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Map common Eastmoney/Sina finance keys onto the cross-check vocabulary."""
    aliases = {
        "assets": ("TOTAL_ASSETS", "total_assets", "assets", "资产总计"),
        "liabilities": ("TOTAL_LIABILITIES", "total_liabilities", "liabilities", "负债合计"),
        "equity": ("TOTAL_EQUITY", "total_equity", "equity", "所有者权益合计", "股东权益合计"),
        "net_profit": ("NETPROFIT", "net_profit", "PARENT_NETPROFIT", "净利润"),
        "total_profit": ("TOTAL_PROFIT", "total_profit", "利润总额"),
        "income_tax": ("INCOME_TAX", "income_tax", "所得税费用"),
        "ending_cash": ("END_CASH", "ending_cash", "END_CCE", "期末现金及现金等价物余额"),
        "beginning_cash": ("BEGIN_CASH", "beginning_cash", "BEGIN_CCE", "期初现金及现金等价物余额"),
        "net_cash_increase": ("NETCASH_INCREASE", "net_cash_increase", "CCE_ADD", "现金及现金等价物净增加额"),
    }
    values: dict[str, Any] = {}
    for target, keys in aliases.items():
        for key in keys:
            if key in report and report[key] not in (None, "", "-"):
                values[target] = report[key]
                break
    return values
