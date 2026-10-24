"""A-share ticker normalisation and market routing."""

from __future__ import annotations

import datetime as _dt
import re
from typing import Literal

Market = Literal["sh", "sz", "bj"]

_CODE_RE = re.compile(r"(\d{6})")
_SH_INDEX = {"000001", "000300", "000905", "000016", "000688", "000852", "000010"}
_INDEX_PREFIXES = ("000300", "000905", "000016", "000688", "000852", "000010", "399", "899")


def normalize_code(code: str) -> str:
    """Accept ``sh600000``, ``600000.SH``, ``1.600000`` or ``600000``."""
    if code is None:
        raise ValueError("code is required")
    raw = str(code).strip().upper()
    if not raw:
        raise ValueError("code is required")
    if "." in raw and raw.split(".")[0].isdigit() and raw.split(".")[-1] in {"SH", "SZ", "BJ", "XSHG", "XSHE", "XBJ"}:
        raw = raw.split(".")[0]
    m = _CODE_RE.search(raw)
    if not m:
        raise ValueError(f"invalid A-share code: {code}")
    return m.group(1)


def market_of(code: str) -> Market:
    c = normalize_code(code)
    raw = str(code).strip().lower()
    if raw.startswith(("sh", "1.")):
        return "sh"
    if raw.startswith(("sz", "0.")) and not raw.startswith("0.9"):
        return "sz"
    if raw.startswith(("bj", "2.")):
        return "bj"
    if c.startswith(("92", "43", "83", "87", "88")):
        return "bj"
    if c.startswith(("5", "6", "9")):
        return "sh"
    if c.startswith(("4", "8")):
        return "bj"
    return "sz"


def tencent_symbol(code: str) -> str:
    c = normalize_code(code)
    raw = str(code).strip().lower()
    if raw.startswith(("sh", "sz", "bj")) and not raw[:2].isdigit():
        return raw[:2] + c
    return market_of(code) + c


def eastmoney_secid(code: str, index: bool = False) -> str:
    c = normalize_code(code)
    m = market_of(code)
    if index and m == "sh":
        return f"1.{c}"
    if index and m == "sz":
        return f"0.{c}"
    return f"{'1' if m == 'sh' else '0'}.{c}"


def sina_symbol(code: str) -> str:
    return tencent_symbol(code)


def is_index(code: str, explicit_index: bool | None = None) -> bool:
    if explicit_index is not None:
        return explicit_index
    c = normalize_code(code)
    raw = str(code).strip().lower()
    if raw.startswith(("sh", "1.")) and c.startswith("000"):
        return True
    return c.startswith(_INDEX_PREFIXES)


def to_joinquant(code: str) -> str:
    c = normalize_code(code)
    m = market_of(code)
    suffix = {"sh": "XSHG", "sz": "XSHE", "bj": "XBJ"}[m]
    return f"{c}.{suffix}"


def parse_date(value: str | _dt.date | _dt.datetime | None, default: str | None = None) -> str:
    """Return ``YYYY-MM-DD``; accepts compact ``YYYYMMDD``."""
    if value is None:
        if default is not None:
            return default
        return _dt.date.today().isoformat()
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    raise ValueError(f"invalid date: {value} (expected YYYY-MM-DD or YYYYMMDD)")


def compact_date(value: str | _dt.date | None = None) -> str:
    return parse_date(value).replace("-", "")


def today() -> str:
    return _dt.date.today().isoformat()


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")


def ensure_code_list(codes: str | list[str]) -> list[str]:
    if isinstance(codes, str):
        parts = [p.strip() for p in re.split(r"[,;\s]+", codes) if p.strip()]
    else:
        parts = list(codes)
    return [normalize_code(c) for c in parts]
