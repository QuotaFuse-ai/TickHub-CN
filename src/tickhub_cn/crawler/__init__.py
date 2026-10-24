"""Offline crawler: watchlist jobs, SQLite archive, cron daemon, text cleaning."""

from .cleaner import clean_text, strip_html
from .daemon import daemon_status, read_state, request_stop, run_daemon, state_path
from .jobs import (
    TASKS,
    TaskResult,
    archive_announcements,
    crawl_announcements,
    crawl_reports,
    crawl_risks,
    crawl_status,
    resolve_watchlist,
    run_task,
)
from .store import Archive, get_archive, item_key, reset_archive

__all__ = [
    "TASKS",
    "Archive",
    "TaskResult",
    "archive_announcements",
    "clean_text",
    "crawl_announcements",
    "crawl_reports",
    "crawl_risks",
    "crawl_status",
    "daemon_status",
    "get_archive",
    "item_key",
    "read_state",
    "request_stop",
    "reset_archive",
    "resolve_watchlist",
    "run_daemon",
    "run_task",
    "state_path",
    "strip_html",
]
