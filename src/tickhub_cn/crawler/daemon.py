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
from ..datasources.util import now_shanghai
from ..logging.setup import get_logger
from . import event_publisher
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


def _event_tick() -> None:
    """Drain the event queue and outbox, then report liveness between rounds."""
    try:
        # Draining first keeps parked data events ahead of the idle heartbeat.
        event_publisher.flush_outbox()
    except Exception as exc:
        _log().warning(
            "event outbox flush failed",
            extra={"event": "event_outbox_flush_failed", "error_code": "QFSD-1001", "error": str(exc)[:200]},
        )
    finished = now_shanghai()
    try:
        event_publisher.heartbeat(
            provider="tickhub-cn",
            market="CN",
            # Idle runs must not collide with the once-per-day crawl rounds.
            run_id=f"cn-{finished:%Y%m%d}-idle-{finished:%H%M}",
            status="ok",
            items=0,
            finished_at=finished,
        )
        event_publisher.wait_idle()
    except Exception as exc:
        _log().warning(
            "idle heartbeat failed",
            extra={"event": "event_heartbeat_failed", "error_code": "QFSD-1001", "error": str(exc)[:200]},
        )


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

    wants_northbound = task in ("all", "northbound")
    northbound_trigger = None
    if wants_northbound:
        try:
            northbound_trigger = CronTrigger.from_crontab(
                settings.offline_crawler_northbound_cron, timezone="Asia/Shanghai"
            )
        except Exception as exc:
            _log().error(
                "invalid OFFLINE_CRAWLER_NORTHBOUND_CRON",
                extra={"event": "crawler_cron_invalid", "error_code": "QFSD-3001", "error": str(exc)[:200]},
            )
            return 3

    # Market collectors only join the full daemon. Their crons are tied to the
    # post-close window, and a single-task daemon already runs its task on the
    # main cron, so adding these would duplicate every round.
    market_jobs: list[tuple[str, str, str, Any]] = []
    if task == "all":
        for job_id, task_name, env_name, cron in (
            (
                "offline_crawl_financials",
                "financials",
                "OFFLINE_CRAWLER_FINANCIALS_CRON",
                settings.offline_crawler_financials_cron,
            ),
            (
                "offline_crawl_financials_season",
                "financials",
                "OFFLINE_CRAWLER_FINANCIALS_SEASON_CRON",
                settings.offline_crawler_financials_season_cron,
            ),
            ("offline_crawl_kline", "kline", "OFFLINE_CRAWLER_KLINE_CRON", settings.offline_crawler_kline_cron),
            ("offline_crawl_quote", "quote", "OFFLINE_CRAWLER_QUOTE_CRON", settings.offline_crawler_quote_cron),
        ):
            try:
                market_jobs.append((job_id, task_name, cron, CronTrigger.from_crontab(cron, timezone="Asia/Shanghai")))
            except Exception as exc:
                _log().error(
                    f"invalid {env_name}",
                    extra={
                        "event": "crawler_cron_invalid",
                        "error_code": "QFSD-3001",
                        "detail": {"env": env_name},
                        "error": str(exc)[:200],
                    },
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
    if northbound_trigger is not None:
        # The session-time snapshot cannot ride the nightly job: kamt is closed
        # (status 3, values zeroed or quota placeholders) long before 02:00.
        scheduler.add_job(
            _tick,
            northbound_trigger,
            args=["northbound"],
            id="offline_crawl_northbound",
            name="offline crawl (northbound)",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
            replace_existing=True,
        )
    for job_id, task_name, _cron, trigger_ in market_jobs:
        # One cron per market task: a slow financial sweep must never delay the
        # announcement round or the post-close kline/quote snapshots.
        scheduler.add_job(
            _tick,
            trigger_,
            args=[task_name],
            id=job_id,
            name=f"offline crawl ({task_name})",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
            replace_existing=True,
        )
    if settings.event_publish_enable:
        # Only exists when the FinanceCoffer side channel is on: a disabled
        # publisher has nothing to flush and must not add jobs to the schedule.
        scheduler.add_job(
            _event_tick,
            "interval",
            minutes=5,
            id="event_tick",
            name="event outbox flush / idle heartbeat",
            coalesce=True,
            max_instances=1,
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
    def _next_fire(trig: Any) -> str | None:
        try:
            nxt = trig.get_next_fire_time(None, _dt.datetime.now(trig.timezone))
            return nxt.isoformat(timespec="seconds") if nxt else None
        except Exception:
            return None

    next_run = _next_fire(trigger)
    northbound_next = _next_fire(northbound_trigger) if northbound_trigger is not None else None
    northbound_cron = settings.offline_crawler_northbound_cron if wants_northbound else ""
    market_state: dict[str, Any] = {
        "financials_cron": "",
        "financials_season_cron": "",
        "kline_cron": "",
        "quote_cron": "",
    }
    for job_id, _task_name, cron, trigger_ in market_jobs:
        prefix = job_id.removeprefix("offline_crawl_")
        market_state[f"{prefix}_cron"] = cron
        market_state[f"{prefix}_next_run_at"] = _next_fire(trigger_)
    write_state(
        pid=os.getpid(),
        cron=settings.offline_crawler_cron,
        northbound_cron=northbound_cron,
        task=task,
        started_at=_dt.datetime.now().isoformat(timespec="seconds"),
        next_run_at=next_run,
        northbound_next_run_at=northbound_next,
        **market_state,
    )
    _log().info(
        "crawler daemon started",
        extra={
            "event": "crawler_start",
            "detail": {
                "cron": settings.offline_crawler_cron,
                "northbound_cron": northbound_cron,
                "task": task,
                "run_on_start": should_run,
                "timezone": "Asia/Shanghai",
                "next_run_at": next_run,
                "northbound_next_run_at": northbound_next,
                "market_state": market_state,
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
        "northbound_cron": state.get("northbound_cron", ""),
        "northbound_next_run_at": state.get("northbound_next_run_at"),
        "financials_cron": state.get("financials_cron", ""),
        "financials_next_run_at": state.get("financials_next_run_at"),
        "financials_season_cron": state.get("financials_season_cron", ""),
        "financials_season_next_run_at": state.get("financials_season_next_run_at"),
        "kline_cron": state.get("kline_cron", ""),
        "kline_next_run_at": state.get("kline_next_run_at"),
        "quote_cron": state.get("quote_cron", ""),
        "quote_next_run_at": state.get("quote_next_run_at"),
        "last_error": state.get("last_error", ""),
        "updated_at": state.get("updated_at"),
    }
    return out


def stop_requested() -> bool:
    return _stop.is_set()


__all__ = ["daemon_status", "read_state", "request_stop", "run_daemon", "state_path", "stop_requested", "write_state"]
