"""CLI formatting: JSON by default, plus table / csv / yaml."""

from __future__ import annotations

import csv
import io
import json
import sys
from typing import Any, Iterable, TextIO

FORMATS = ("json", "table", "csv", "yaml")


def rows_of(payload: Any) -> list[dict[str, Any]]:
    """Pick the first meaningful list of records from an envelope."""
    data = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        lists = [
            value
            for key, value in data.items()
            if isinstance(value, list) and value and isinstance(value[0], dict) and key not in ("failures",)
        ]
        if lists:
            return lists[0]
        return [data]
    return []


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _write_json(payload: Any, stream: TextIO) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")


def _write_csv(rows: list[dict[str, Any]], stream: TextIO) -> None:
    if not rows:
        return
    headers: list[str] = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _flatten(row.get(k)) for k in headers})
    stream.write(buffer.getvalue())


def _write_table(rows: list[dict[str, Any]], stream: TextIO) -> None:
    if not rows:
        return
    headers: list[str] = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    try:
        from rich.console import Console
        from rich.table import Table

        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        for header in headers:
            table.add_column(header, overflow="fold", max_width=48)
        for row in rows[:500]:
            table.add_row(*[_flatten(row.get(h)) for h in headers])
        Console(file=stream, soft_wrap=False, width=200).print(table)
        if len(rows) > 500:
            Console(file=stream).print(f"... {len(rows) - 500} more rows", style="dim")
        return
    except Exception:
        pass

    widths = {h: min(max(len(h), *(len(_flatten(r.get(h))) for r in rows[:200])), 48) for h in headers}
    stream.write("  ".join(h[: widths[h]].ljust(widths[h]) for h in headers) + "\n")
    stream.write("  ".join("-" * widths[h] for h in headers) + "\n")
    for row in rows[:500]:
        stream.write("  ".join(_flatten(row.get(h))[: widths[h]].ljust(widths[h]) for h in headers) + "\n")
    if len(rows) > 500:
        stream.write(f"... {len(rows) - 500} more rows\n")


def _write_yaml(payload: Any, stream: TextIO) -> None:
    try:
        import yaml
    except Exception:
        sys.stderr.write("yaml output unavailable (install PyYAML), falling back to json\n")
        _write_json(payload, stream)
        return
    stream.write(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False))


def emit(payload: Any, output: str = "json", *, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    fmt = (output or "json").lower()
    if fmt == "json":
        _write_json(payload, out)
    elif fmt == "table":
        _write_table(rows_of(payload), out)
    elif fmt == "csv":
        _write_csv(rows_of(payload), out)
    elif fmt == "yaml":
        _write_yaml(payload, out)
    else:
        raise ValueError(f"unsupported output format: {output} (expected {', '.join(FORMATS)})")
    try:
        out.flush()
    except Exception:
        pass


def emit_error(err: dict[str, Any], output: str = "json") -> None:
    emit({"ok": False, "error": err}, output="json")


__all__ = ["FORMATS", "emit", "emit_error", "rows_of"]
