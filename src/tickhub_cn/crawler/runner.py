"""In-process job registry for console-triggered crawls (spec §4.12).

``POST /api/crawler/run`` must answer immediately: a crawl over a watchlist can
take minutes. Jobs run on a daemon thread here, with live progress, a cancel
flag, and one-at-a-time admission so two submits cannot hammer the same sources.

The registry is process-local and bounded. Durable history lives in the
archive's ``crawl_task`` table; this module only carries the operational state
(progress, cancellation, the result the caller has not read yet).
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
from typing import Any

from ..logging.setup import get_logger
from .jobs import TASKS, run_task

HISTORY_LIMIT = 50
ACTIVE_STATUSES = ("queued", "running")


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _log() -> Any:
    return get_logger("crawler")


class JobBusy(RuntimeError):
    """Another crawl is queued/running; ``job`` is its snapshot."""

    def __init__(self, job: dict[str, Any]):
        super().__init__("a crawl job is already running")
        self.job = job


class JobNotFound(KeyError):
    pass


class _Job:
    def __init__(
        self,
        job_id: int,
        task: str,
        *,
        codes: Any,
        days: int,
        limit: int | None,
        concurrency: int | None,
    ) -> None:
        self.id = job_id
        self.task = task
        self.codes = codes
        self.days = days
        self.limit = limit
        self.concurrency = concurrency
        self.status = "queued"
        self.created_at = _now()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.started_monotonic = 0.0
        self.finished_monotonic = 0.0
        self.stop = threading.Event()
        self.cancel_requested = False
        self.error = ""
        self.result: dict[str, Any] | None = None
        self.codes_total = 0
        self.steps_total = 0
        self.steps_done = 0
        self.current = ""
        self.failed_ticks = 0
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    # ------------------------------------------------------------- progress

    def tick(self, payload: dict[str, Any]) -> None:
        with self.lock:
            if payload.get("phase") == "start":
                self.codes_total = int(payload.get("codes") or 0)
                tasks = payload.get("tasks") or []
                self.steps_total = self.codes_total * max(1, len(tasks))
                return
            if "code" in payload:
                self.steps_done += 1
                self.current = f"{payload.get('task')}:{payload.get('code')}"
                if payload.get("failed"):
                    self.failed_ticks += 1

    # -------------------------------------------------------------- lifecycle

    def start(self) -> bool:
        self.started_at = _now()
        with self.lock:
            if self.stop.is_set():
                self.status = "cancelled"
                self.finished_at = _now()
                self.finished_monotonic = time.monotonic()
                return False
            self.status = "running"
            self.started_monotonic = time.monotonic()
            return True

    def finish(self, result: dict[str, Any]) -> None:
        with self.lock:
            self.result = result
            self.status = str(result.get("status") or "ok")
            self.finished_at = _now()
            self.finished_monotonic = time.monotonic()
            if self.steps_total and self.status not in ("cancelled", "failed"):
                self.steps_done = self.steps_total

    def fail(self, message: str) -> None:
        with self.lock:
            self.error = message
            self.status = "failed"
            self.finished_at = _now()
            self.finished_monotonic = time.monotonic()

    def cancel(self) -> None:
        self.stop.set()
        with self.lock:
            self.cancel_requested = True
            if self.status == "queued":
                self.status = "cancelled"
                self.finished_at = _now()
                self.finished_monotonic = time.monotonic()

    # --------------------------------------------------------------- reading

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            total = self.steps_total
            done = min(self.steps_done, total) if total else self.steps_done
            if total:
                percent = int(round(100 * done / total))
            else:
                percent = 100 if self.status not in ACTIVE_STATUSES else 0
            if self.started_monotonic:
                end = self.finished_monotonic or time.monotonic()
                elapsed = round(end - self.started_monotonic, 1)
            else:
                elapsed = 0.0
            return {
                "id": self.id,
                "task": self.task,
                "status": self.status,
                "codes": self.codes_total,
                "codes_param": self.codes,
                "days": self.days,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_s": elapsed,
                "cancel_requested": self.cancel_requested,
                "progress": {
                    "done": done,
                    "total": total,
                    "percent": percent,
                    "current": self.current,
                    "failed": self.failed_ticks,
                },
                "result": self.result,
                "error": self.error,
            }


_JOBS: dict[int, _Job] = {}
_LOCK = threading.RLock()
_RUNNING: _Job | None = None
_SEQ = 0


def _running_locked() -> _Job | None:
    global _RUNNING
    if _RUNNING is not None and _RUNNING.status not in ACTIVE_STATUSES:
        _RUNNING = None
    return _RUNNING


def _trim_locked() -> None:
    if len(_JOBS) <= HISTORY_LIMIT:
        return
    for job_id in list(_JOBS):
        if len(_JOBS) <= HISTORY_LIMIT:
            break
        if _JOBS[job_id].status not in ACTIVE_STATUSES:
            del _JOBS[job_id]


def _normalize_task(task: str | None) -> str:
    name = (task or "all").strip().lower()
    if name != "all" and name not in TASKS:
        raise ValueError(f"unknown crawl task: {task} (expected one of {', '.join(TASKS + ('all',))})")
    return name


def _run(job: _Job) -> None:
    if not job.start():
        return
    try:
        result = run_task(
            job.task,
            codes=job.codes,
            days=job.days,
            limit=job.limit,
            concurrency=job.concurrency,
            progress=job.tick,
            should_stop=job.stop.is_set,
        )
    except Exception as exc:
        job.fail(str(exc)[:500])
        _log().error(
            "crawl job failed",
            extra={
                "event": "crawl_job_failed",
                "error_code": "QFSD-1001",
                "error": str(exc)[:200],
                "detail": {"job": job.id, "task": job.task},
            },
        )
        return
    job.finish(result)
    _log().info(
        "crawl job finished",
        extra={
            "event": "crawl_job_done",
            "detail": {"job": job.id, "task": job.task, "status": job.status},
        },
    )


def submit_job(
    task: str = "all",
    *,
    codes: Any = None,
    days: int = 7,
    limit: int | None = None,
    concurrency: int | None = None,
) -> dict[str, Any]:
    """Start a crawl on a daemon thread and return its snapshot immediately."""
    global _RUNNING, _SEQ
    name = _normalize_task(task)
    with _LOCK:
        running = _running_locked()
        if running is not None:
            raise JobBusy(running.snapshot())
        _SEQ += 1
        job = _Job(_SEQ, name, codes=codes, days=days, limit=limit, concurrency=concurrency)
        _JOBS[job.id] = job
        _RUNNING = job
        _trim_locked()
    job.thread = threading.Thread(target=_run, args=(job,), name=f"crawl-job-{job.id}", daemon=True)
    job.thread.start()
    return job.snapshot()


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    with _LOCK:
        ids = sorted(_JOBS, reverse=True)[: max(1, int(limit))]
        return [_JOBS[job_id].snapshot() for job_id in ids]


def get_job(job_id: int) -> dict[str, Any]:
    with _LOCK:
        job = _JOBS.get(int(job_id))
    if job is None:
        raise JobNotFound(job_id)
    return job.snapshot()


def running_job() -> dict[str, Any] | None:
    with _LOCK:
        job = _running_locked()
        return job.snapshot() if job is not None else None


def cancel_job(job_id: int) -> dict[str, Any]:
    """Ask a job to stop; work already in flight finishes its current request.

    Rows that already landed stay in the archive — a cancelled run is a partial
    pre-warm, never a rollback.
    """
    with _LOCK:
        job = _JOBS.get(int(job_id))
    if job is None:
        raise JobNotFound(job_id)
    job.cancel()
    return job.snapshot()


def retry_job(job_id: int) -> dict[str, Any]:
    """Submit a new job with the same parameters as a finished one."""
    with _LOCK:
        job = _JOBS.get(int(job_id))
    if job is None:
        raise JobNotFound(job_id)
    if job.status in ACTIVE_STATUSES:
        raise JobBusy(job.snapshot())
    return submit_job(
        job.task,
        codes=job.codes,
        days=job.days,
        limit=job.limit,
        concurrency=job.concurrency,
    )


def reset_jobs() -> None:
    """Drop all job records (tests; nothing durable is lost)."""
    global _RUNNING, _SEQ
    with _LOCK:
        _JOBS.clear()
        _RUNNING = None
        _SEQ = 0


__all__ = [
    "ACTIVE_STATUSES",
    "HISTORY_LIMIT",
    "JobBusy",
    "JobNotFound",
    "cancel_job",
    "get_job",
    "list_jobs",
    "reset_jobs",
    "retry_job",
    "running_job",
    "submit_job",
]
