"""CL-08/CL-10: console-triggered crawls run as polled background jobs.

The admin route must answer immediately and expose progress, cancellation and
retry; the archive keeps whatever landed before a cancel (a cancelled run is a
partial pre-warm, never a rollback).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tickhub_cn.crawler import runner


@pytest.fixture(autouse=True)
def clean_jobs() -> Any:
    runner.reset_jobs()
    yield
    runner.reset_jobs()


def wait_for(check: Any, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("job did not reach the expected state in time")


def _job(job_id: int, status: str | tuple[str, ...] = ()) -> dict[str, Any] | None:
    snapshot = runner.get_job(job_id)
    if not status:
        return snapshot
    wanted = (status,) if isinstance(status, str) else status
    return snapshot if snapshot["status"] in wanted else None


def test_submit_answers_before_the_crawl_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def fake_run_task(
        task: str = "all",
        *,
        codes: Any = None,
        days: int = 7,
        limit: int | None = None,
        concurrency: int | None = None,
        progress: Any = None,
        should_stop: Any = None,
    ) -> dict[str, Any]:
        progress({"phase": "start", "task": task, "codes": 2, "tasks": ["announcements"]})
        progress({"task": "announcements", "code": "600000", "fetched": 1, "inserted": 1})
        assert release.wait(5)
        return {"task": task, "status": "ok", "codes": 2, "fetched": 1, "inserted": 1, "failed": []}

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    job = runner.submit_job("announcements", codes=["600000", "000001"], days=3)
    # The fake blocks on ``release``, so a synchronous submit would return finished.
    assert job["status"] in ("queued", "running")
    assert job["finished_at"] is None and job["result"] is None

    progressed = wait_for(lambda: (lambda s: s if s["progress"]["done"] else None)(runner.get_job(job["id"])))
    assert progressed["progress"]["total"] == 2
    assert progressed["progress"]["percent"] == 50
    assert progressed["progress"]["current"] == "announcements:600000"

    release.set()
    done = wait_for(lambda: _job(job["id"], "ok"))
    assert done["result"]["inserted"] == 1
    assert done["progress"]["percent"] == 100
    assert done["finished_at"]


def test_cancel_stops_between_codes_and_keeps_partial_work(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    observed: dict[str, Any] = {}

    def fake_run_task(
        task: str = "all",
        *,
        codes: Any = None,
        days: int = 7,
        limit: int | None = None,
        concurrency: int | None = None,
        progress: Any = None,
        should_stop: Any = None,
    ) -> dict[str, Any]:
        progress({"phase": "start", "task": task, "codes": 3, "tasks": ["announcements"]})
        inserted = 0
        for code in ("600000", "000001", "000002"):
            if should_stop():
                observed["stopped_at"] = code
                return {
                    "task": task, "status": "cancelled", "cancelled": True,
                    "codes": 3, "fetched": inserted, "inserted": inserted, "failed": [],
                }
            inserted += 1
            progress({"task": "announcements", "code": code, "fetched": 1, "inserted": 1})
            started.set()
            time.sleep(0.05)
        return {"task": task, "status": "ok", "codes": 3, "fetched": inserted, "inserted": inserted, "failed": []}

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    job = runner.submit_job("announcements", codes=["600000", "000001", "000002"])
    assert started.wait(5)
    cancelled = runner.cancel_job(job["id"])
    assert cancelled["cancel_requested"] is True

    final = wait_for(lambda: _job(job["id"], "cancelled"))
    assert observed.get("stopped_at") in ("000001", "000002")
    assert final["result"]["cancelled"] is True
    assert final["result"]["inserted"] >= 1
    assert final["progress"]["percent"] < 100


def test_only_one_job_runs_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def fake_run_task(task: str = "all", **_: Any) -> dict[str, Any]:
        assert release.wait(5)
        return {"task": task, "status": "ok", "codes": 0, "fetched": 0, "inserted": 0, "failed": []}

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    first = runner.submit_job("risks")
    with pytest.raises(runner.JobBusy) as busy:
        runner.submit_job("risks")
    assert busy.value.job["id"] == first["id"]

    release.set()
    wait_for(lambda: _job(first["id"], "ok"))
    second = runner.submit_job("risks")
    assert second["id"] != first["id"]
    wait_for(lambda: _job(second["id"], "ok"))


def test_retry_resubmits_the_same_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    calls: list[tuple[str, Any, int]] = []

    def fake_run_task(task: str = "all", *, codes: Any = None, days: int = 7, **_: Any) -> dict[str, Any]:
        calls.append((task, codes, days))
        release.wait(5)
        return {"task": task, "status": "failed", "codes": 1, "fetched": 0, "inserted": 0, "failed": ["600000"]}

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    job = runner.submit_job("announcements", codes=["600000"], days=14)
    wait_for(lambda: _job(job["id"], "running"))

    with pytest.raises(runner.JobBusy):
        runner.retry_job(job["id"])

    release.set()
    wait_for(lambda: _job(job["id"], "failed"))

    retried = runner.retry_job(job["id"])
    assert retried["id"] != job["id"]
    assert retried["days"] == 14
    wait_for(lambda: _job(retried["id"], "failed"))
    assert calls == [("announcements", ["600000"], 14), ("announcements", ["600000"], 14)]

    with pytest.raises(runner.JobNotFound):
        runner.retry_job(999_999)


def test_unknown_task_is_rejected_before_a_thread_starts() -> None:
    with pytest.raises(ValueError):
        runner.submit_job("nope")
    assert runner.list_jobs() == []
    assert runner.running_job() is None


def test_run_task_stops_between_codes_and_keeps_the_rows_already_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tickhub_cn.datasources as datasources
    from tickhub_cn.crawler import jobs
    from tickhub_cn.crawler.store import get_archive

    def fake_call(name: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        code = str(params.get("code") or "")
        if name == "cninfo_announcements":
            return {
                "ok": True,
                "data": {
                    "announcements": [
                        {
                            "code": code,
                            "title": f"关于回购公司股份的公告 {code}",
                            "publish_time": "2026-10-20",
                            "id": f"ID-{code}",
                        }
                    ]
                },
            }
        raise AssertionError(f"unexpected tool call: {name}")

    monkeypatch.setattr(datasources, "call_tool", fake_call)
    stop = {"now": False}

    def progress(payload: dict[str, Any]) -> None:
        if payload.get("code"):
            stop["now"] = True  # cancel as soon as the first code lands

    result = jobs.run_task(
        "all",
        codes=["600000", "000001"],
        days=3,
        concurrency=1,
        progress=progress,
        should_stop=lambda: stop["now"],
    )

    assert result["status"] == "cancelled"
    assert result["cancelled"] is True
    assert [t["task"] for t in result["tasks"]] == ["announcements"]
    assert result["inserted"] == 1
    assert get_archive().stats()["announcement"] == 1


def _client(**overrides: Any) -> Any:
    from fastapi.testclient import TestClient

    from tickhub_cn.admin_api.app import create_app
    from tickhub_cn.core.config import get_settings

    web_dir = Path(__file__).resolve().parents[2] / "web" / "admin"
    admin = {"auth_token": "t0ken", "readonly": False, "web_dir": str(web_dir)}
    admin.update(overrides)
    return TestClient(create_app(get_settings(), admin=admin))


def _headers() -> dict[str, str]:
    return {"x-admin-token": "t0ken"}


def test_admin_routes_submit_list_cancel_and_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def fake_run_task(
        task: str = "all",
        *,
        codes: Any = None,
        days: int = 7,
        limit: int | None = None,
        concurrency: int | None = None,
        progress: Any = None,
        should_stop: Any = None,
    ) -> dict[str, Any]:
        progress({"phase": "start", "task": task, "codes": 1, "tasks": ["announcements"]})
        release.wait(5)
        return {
            "task": task, "status": "cancelled" if should_stop() else "ok",
            "cancelled": should_stop(), "codes": 1, "fetched": 1 if should_stop() else 0,
            "inserted": 0, "failed": [],
        }

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    client = _client()

    submitted = client.post("/api/crawler/run", headers=_headers(), json={"task": "announcements", "codes": ["600000"]})
    assert submitted.status_code == 200, submitted.text
    job = submitted.json()["job"]
    assert job["task"] == "announcements"
    assert job["status"] in ("queued", "running")

    again = client.post("/api/crawler/run", headers=_headers(), json={"task": "risks"})
    assert again.status_code == 409
    assert again.json()["job"]["id"] == job["id"]

    listed = client.get("/api/crawler/jobs", headers=_headers()).json()
    assert listed["running"] == job["id"]
    assert [row["id"] for row in listed["jobs"]] == [job["id"]]

    one = client.get(f"/api/crawler/jobs/{job['id']}", headers=_headers())
    assert one.status_code == 200
    assert one.json()["job"]["id"] == job["id"]
    assert client.get("/api/crawler/jobs/999999", headers=_headers()).status_code == 404

    cancelled = client.post(f"/api/crawler/jobs/{job['id']}/cancel", headers=_headers())
    assert cancelled.status_code == 200
    assert cancelled.json()["job"]["cancel_requested"] is True
    assert client.post("/api/crawler/jobs/999999/cancel", headers=_headers()).status_code == 404

    release.set()
    wait_for(lambda: _job(job["id"], "cancelled"))

    retried = client.post(f"/api/crawler/jobs/{job['id']}/retry", headers=_headers())
    assert retried.status_code == 200, retried.text
    new_job = retried.json()["job"]
    assert new_job["id"] != job["id"]
    release.set()
    wait_for(lambda: _job(new_job["id"], ("ok", "cancelled")))

    bad = client.post("/api/crawler/run", headers=_headers(), json={"task": "nope"})
    assert bad.status_code == 422


def test_readonly_allows_job_reads_but_blocks_submit_cancel_and_retry() -> None:
    client = _client(readonly=True)
    assert client.get("/api/crawler/jobs", headers=_headers()).status_code == 200
    assert client.post("/api/crawler/run", headers=_headers(), json={"task": "risks"}).status_code == 403
    assert client.post("/api/crawler/jobs/1/cancel", headers=_headers()).status_code == 403
    assert client.post("/api/crawler/jobs/1/retry", headers=_headers()).status_code == 403
