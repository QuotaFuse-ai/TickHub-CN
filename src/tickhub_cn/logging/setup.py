"""Structured logging. stdout is reserved for MCP frames and CLI payloads."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

from ..core.config import Settings, get_settings

SECRET_PATTERNS = [
    re.compile(r"(api[_-]?key|token|password|secret)([\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)", re.I),
    re.compile(r"(redis://[^:]+:)([^@]+)(@)", re.I),
]


def redact(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    out = text
    for pat in SECRET_PATTERNS:
        out = pat.sub(lambda m: m.group(1) + (m.group(2) if m.lastindex and m.lastindex >= 2 else "") + "***" + (m.group(3) if m.lastindex and m.lastindex >= 3 else ""), out)
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in ("trace_id", "module", "source", "error_code", "elapsed_ms", "event", "detail"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class SqliteHandler(logging.Handler):
    _lock = threading.Lock()

    def __init__(self, path: str):
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS log_entry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT, level TEXT, logger TEXT, message TEXT, payload TEXT
                )"""
            )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = json.loads(self.format(record))
            with self._lock, sqlite3.connect(self.path) as con:
                con.execute(
                    "INSERT INTO log_entry (ts, level, logger, message, payload) VALUES (?,?,?,?,?)",
                    (data.get("ts"), data.get("level"), data.get("logger"), data.get("message"), json.dumps(data, ensure_ascii=False)),
                )
        except Exception:  # pragma: no cover
            self.handleError(record)


class DatabaseHandler(logging.Handler):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self._engine = None
        try:
            from sqlalchemy import create_engine, text

            url = self._url()
            self._engine = create_engine(url, future=True)
            with self._engine.begin() as con:
                con.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS log_entry ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts VARCHAR(40), level VARCHAR(10), "
                        "logger VARCHAR(80), message TEXT, payload TEXT)"
                    )
                )
            self._text = text
        except Exception as exc:  # pragma: no cover - optional dependency
            sys.stderr.write(json.dumps({"level": "warn", "event": "log_db_disabled", "error": str(exc)}) + "\n")

    def _url(self) -> str:
        s = self.settings
        if s.log_sink == "mysql":
            return f"mysql+pymysql://{s.log_db_user}:{s.log_db_password}@{s.log_db_host}:{s.log_db_port or 3306}/{s.log_db_name}"
        return f"postgresql+psycopg2://{s.log_db_user}:{s.log_db_password}@{s.log_db_host}:{s.log_db_port or 5432}/{s.log_db_name}"

    def emit(self, record: logging.LogRecord) -> None:
        if self._engine is None:
            return
        try:
            data = json.loads(self.format(record))
            with self._engine.begin() as con:
                con.execute(
                    self._text(
                        "INSERT INTO log_entry (ts, level, logger, message, payload) VALUES (:ts,:level,:logger,:message,:payload)"
                    ),
                    {"ts": data.get("ts", ""), "level": data.get("level", ""), "logger": data.get("logger", ""), "message": data.get("message", ""), "payload": json.dumps(data, ensure_ascii=False)},
                )
        except Exception:  # pragma: no cover
            pass


_CONFIGURED = False


def setup_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    s = settings or get_settings()
    logger = logging.getLogger("tickhub")
    logger.setLevel(getattr(logging, s.log_level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    formatter = JsonFormatter()
    sink = s.log_sink
    streams: list[logging.Handler] = []
    if sink in ("stderr", "console", ""):
        streams.append(logging.StreamHandler(sys.stderr))
    if sink == "file":
        Path(s.log_file).parent.mkdir(parents=True, exist_ok=True)
        streams.append(logging.FileHandler(s.log_file, encoding="utf-8"))
        streams.append(logging.StreamHandler(sys.stderr))
    elif sink == "sqlite":
        streams.append(SqliteHandler(s.log_file + ".db"))
        streams.append(logging.StreamHandler(sys.stderr))
    elif sink in ("mysql", "postgres"):
        streams.append(DatabaseHandler(s))
        streams.append(logging.StreamHandler(sys.stderr))
    if sink != "file":
        # The admin console reads this file; a sink that writes elsewhere
        # (stderr, sqlite, mysql) would otherwise leave its log page empty.
        Path(s.log_file).parent.mkdir(parents=True, exist_ok=True)
        streams.append(logging.FileHandler(s.log_file, encoding="utf-8"))
    for h in streams:
        h.setFormatter(formatter)
        logger.addHandler(h)
    _CONFIGURED = True


def get_logger(name: str = "tickhub") -> logging.Logger:
    setup_logging()
    return logging.getLogger(name if name.startswith("tickhub") else f"tickhub.{name}")


def audit(action: str, actor: str = "system", detail: dict[str, Any] | None = None) -> None:
    s = get_settings()
    record = {
        "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "actor": actor,
        "detail": detail or {},
    }
    path = Path("./logs/admin_audit.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    get_logger("admin").info("admin audit", extra={"event": "audit", "detail": record})
