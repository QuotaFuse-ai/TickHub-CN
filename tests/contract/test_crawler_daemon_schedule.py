"""Daemon schedule: the northbound snapshot needs its own post-close slot."""

from __future__ import annotations

import json

import pytest

from tickhub_cn.crawler import daemon


class _FakeScheduler:
    instances: list["_FakeScheduler"] = []

    def __init__(self, timezone=None) -> None:
        self.timezone = timezone
        self.jobs: list[dict] = []
        self.running = False
        _FakeScheduler.instances.append(self)

    def add_job(self, func, trigger, **kwargs) -> None:
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    def start(self) -> None:
        self.running = True

    def shutdown(self, wait: bool = False) -> None:  # noqa: ARG002 - APScheduler API
        self.running = False

    def job_ids(self) -> list[str]:
        return [job["id"] for job in self.jobs]


def _state() -> dict:
    return json.loads(daemon.state_path().read_text(encoding="utf-8"))


@pytest.fixture
def fake_daemon(monkeypatch):
    _FakeScheduler.instances = []
    monkeypatch.setattr("apscheduler.schedulers.blocking.BlockingScheduler", _FakeScheduler)
    monkeypatch.setattr(daemon, "_install_signals", lambda scheduler: None)


def test_northbound_gets_a_second_job_at_session_close(fake_daemon):
    assert daemon.run_daemon(force=True, run_now=False, task="all") == 0

    scheduler = _FakeScheduler.instances[-1]
    assert scheduler.job_ids() == ["offline_crawl", "offline_crawl_northbound"]
    northbound = next(j for j in scheduler.jobs if j["id"] == "offline_crawl_northbound")
    assert northbound["args"] == ["northbound"]

    state = _state()
    assert state["northbound_cron"] == "5 15 * * 1-5"
    assert state["northbound_next_run_at"]


def test_a_single_non_northbound_task_does_not_schedule_it(fake_daemon):
    assert daemon.run_daemon(force=True, run_now=False, task="announcements") == 0

    scheduler = _FakeScheduler.instances[-1]
    assert scheduler.job_ids() == ["offline_crawl"]
    assert _state()["northbound_cron"] == ""


def test_invalid_northbound_cron_is_reported(fake_daemon, monkeypatch):
    from tickhub_cn.core.config import reload_settings

    monkeypatch.setenv("OFFLINE_CRAWLER_NORTHBOUND_CRON", "not a cron")
    reload_settings()

    assert daemon.run_daemon(force=True, run_now=False, task="all") == 3
    assert _FakeScheduler.instances == []
