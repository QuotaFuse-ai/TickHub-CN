"""Structured risk events for downstream alerting."""

from __future__ import annotations

import re
from typing import Any

PATTERNS: list[tuple[str, str, re.Pattern[str], str]] = [
    ("regulatory_penalty", "high", re.compile(r"(立案|处罚|警示函|问询函|监管函|违规)"), "监管处罚/问询"),
    ("share_reduction", "medium", re.compile(r"(减持|拟减持|集中竞价减持)"), "股东减持"),
    ("lockup_expiry", "high", re.compile(r"(解禁|限售股上市流通)"), "限售解禁"),
    ("earnings_loss", "high", re.compile(r"(预亏|亏损|业绩预减|首亏|续亏)"), "业绩预亏"),
    ("goodwill", "medium", re.compile(r"(商誉减值|计提减值)"), "商誉/资产减值"),
    ("delisting_risk", "high", re.compile(r"(退市风险|终止上市|\*ST)"), "退市风险"),
    ("equity_pledge", "medium", re.compile(r"(质押|平仓风险)"), "股权质押"),
    ("litigation", "medium", re.compile(r"(诉讼|仲裁|冻结)"), "诉讼/冻结"),
    ("private_placement", "low", re.compile(r"(定增|非公开发行)"), "定向增发"),
    ("buyback", "low", re.compile(r"(回购)"), "股份回购"),
]

SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}


def classify_announcement(title: str) -> tuple[str, str, str] | None:
    for kind, severity, pattern, label in PATTERNS:
        if pattern.search(title or ""):
            return kind, severity, label
    return None


def from_announcements(code: str, rows: list[dict[str, Any]], *, source: str = "cninfo") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows or []:
        title = str(row.get("title") or row.get("announcementTitle") or row.get("notice_title") or "")
        hit = classify_announcement(title)
        if not hit:
            continue
        kind, severity, label = hit
        out.append(
            {
                "code": code,
                "type": kind,
                "label": label,
                "severity": severity,
                "date": row.get("date") or row.get("announcementTime") or row.get("publish_time") or row.get("NOTICE_DATE"),
                "title": title,
                "url": row.get("url") or row.get("adjunctUrl"),
                "source": source,
            }
        )
    return out


def from_lockups(code: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows or []:
        out.append(
            {
                "code": code,
                "type": "lockup_expiry",
                "label": "限售解禁",
                "severity": "high",
                "date": row.get("FREE_DATE") or row.get("date"),
                "shares": row.get("ABLE_FREE_SHARES") or row.get("FREE_SHARES"),
                "ratio": row.get("FREE_RATIO"),
                "source": "eastmoney",
            }
        )
    return out


def from_forecasts(code: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows or []:
        kind = str(row.get("PREDICT_TYPE") or row.get("predict_type") or "")
        if not re.search(r"(预亏|预减|首亏|续亏|略减)", kind):
            continue
        out.append(
            {
                "code": code,
                "type": "earnings_loss",
                "label": kind or "业绩预告",
                "severity": "high",
                "date": row.get("NOTICE_DATE") or row.get("REPORT_DATE"),
                "change": row.get("ADD_AMP_LOWER") or row.get("INCREASE_LOWER"),
                "source": "eastmoney",
            }
        )
    return out


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    max_severity = "low"
    for event in events:
        counts[event["type"]] = counts.get(event["type"], 0) + 1
        if SEVERITY_WEIGHT.get(event.get("severity", "low"), 1) > SEVERITY_WEIGHT.get(max_severity, 1):
            max_severity = event.get("severity", "low")
    ordered = sorted(events, key=lambda e: (SEVERITY_WEIGHT.get(e.get("severity", "low"), 1), str(e.get("date") or "")), reverse=True)
    return {"total": len(events), "max_severity": max_severity, "counts": counts, "events": ordered}
