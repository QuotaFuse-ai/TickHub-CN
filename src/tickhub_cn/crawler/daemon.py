"""Background crawler daemon (``th-cn crawler daemon``).

One APScheduler cron job, no network port, no state beyond the archive and a
small JSON status file. ``start-all`` forks this module as its child; it also
runs standalone for operators.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import signal
import threading
from pathlib import Path
from typing import Any

from ..core.config import get_settings
from ..logging.setup import get_logger
from .jobs import crawl_status, run_task

_STATE_NAME = "crawler_state.json"
_stop = threading.Event()
_state_lock = threading.Lock()
_scheduler: Any = None


def _log() -> Any:
    return get_logger("crawler")


def state_path() -> Path:
    settings = get_settings()
    return Path(settings.archive_dir) / _STATE_NAME


def read_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(**fields: Any) -> dict[str, Any]:
    with _state_lock:
        state = read_state()
        state.update(fields)
        state["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return state


def _tick(task: str = "all") -> dict[str, Any]:
    started = _dt.datetime.now().isoformat(timespec="seconds")
    try:
        result = run_task(task)
        write_state(last_run_at=started, last_result=result, last_error="", pid=os.getpid())
        return result
    except Exception as exc:
        write_state(last_run_at=started, last_error=str(exc)[:500], pid=os.getpid())
        _log().error(
            "crawler tick failed",
            extra={"event": "crawl_tick_failed", "error_code": "QFSD-1001", "error": str(exc)[:200]},
        )
        raise


def request_stop() -> None:
    """Ask a running daemon to shut down (signal handler or parent process)."""
    _stop.set()
    scheduler = _scheduler
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass


def _install_signals(scheduler: Any) -> None:
    def handler(signum: int, _frame: Any) -> None:
        if _stop.is_set():
            return
        _log().info("crawler stopping", extra={"event": "crawler_stop", "detail": {"signal": signum}})
        request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, AttributeError):  # not the main thread / unsupported
            pass


def run_daemon(*, force: bool = False, run_now: bool | None = None, task: str = "all") -> int:
    """Blocking cron loop. Returns a process exit code."""
    global _scheduler

    settings = get_settings()
    _stop.clear()
    if not settings.offline_crawler_enable and not force:
        _log().warning(
            "offline crawler disabled",
            extra={"event": "crawler_disabled", "error_code": "QFSD-9003", "detail": {"hint": "set OFFLINE_CRAWLER_ENABLE=true"}},
        )
        return 0

    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except Exception as exc:
        _log().error(
            "apscheduler unavailable",
            extra={"event": "crawler_dependency_missing", "error_code": "QFSD-9003", "error": str(exc)[:200]},
        )
        return 3

    try:
        trigger = CronTrigger.from_crontab(settings.offline_crawler_cron, timezone="Asia/Shanghai")
    except Exception as exc:
        _log().error(
            "invalid OFFLINE_CRAWLER_CRON",
            extra={"event": "crawler_cron_invalid", "error_code": "QFSD-3001", "error": str(exc)[:200]},
        )
        return 3

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        _tick,
        trigger,
        args=[task],
        id="offline_crawl",
        name=f"offline crawl ({task})",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    should_run = settings.offline_crawler_run_on_start if run_now is None else bool(run_now)
    if should_run:
        scheduler.add_job(
            _tick,
            "date",
            run_date=_dt.datetime.now() + _dt.timedelta(seconds=2),
            args=[task],
            id="offline_crawl_boot",
            name="offline crawl (startup)",
        )

    _install_signals(scheduler)
    _scheduler = scheduler
    next_run = None
    try:
        nxt = trigger.get_next_fire_time(None, _dt.datetime.now(trigger.timezone))
        next_run = nxt.isoformat(timespec="seconds") if nxt else None
    except Exception:
        pass
    write_state(
        pid=os.getpid(),
        cron=settings.offline_crawler_cron,
        task=task,
        started_at=_dt.datetime.now().isoformat(timespec="seconds"),
        next_run_at=next_run,
    )
    _log().info(
        "crawler daemon started",
        extra={
            "event": "crawler_start",
            "detail": {
                "cron": settings.offline_crawler_cron,
                "task": task,
                "run_on_start": should_run,
                "timezone": "Asia/Shanghai",
                "next_run_at": next_run,
            },
        },
    )

    try:
        if not _stop.is_set():
            scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        _scheduler = None
        if scheduler.running:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                pass
        write_state(pid=0, stopped_at=_dt.datetime.now().isoformat(timespec="seconds"))
        _log().info("crawler daemon stopped", extra={"event": "crawler_stopped"})
    return 0


def daemon_status() -> dict[str, Any]:
    state = read_state()
    out = crawl_status()
    out["daemon"] = {
        "state_file": str(state_path()),
        "pid": state.get("pid", 0),
        "started_at": state.get("started_at"),
        "last_run_at": state.get("last_run_at"),
        "next_run_at": state.get("next_run_at"),
        "last_error": state.get("last_error", ""),
        "updated_at": state.get("updated_at"),
    }
    return out


def stop_requested() -> bool:
    return _stop.is_set()


__all__ = ["daemon_status", "read_state", "request_stop", "run_daemon", "state_path", "stop_requested", "write_state"]
