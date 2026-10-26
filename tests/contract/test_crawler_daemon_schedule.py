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


MARKET_JOB_IDS = [
    "offline_crawl_financials",
    "offline_crawl_financials_season",
    "offline_crawl_kline",
    "offline_crawl_quote",
]


def test_northbound_gets_a_second_job_at_session_close(fake_daemon):
    assert daemon.run_daemon(force=True, run_now=False, task="all") == 0

    scheduler = _FakeScheduler.instances[-1]
    assert scheduler.job_ids() == ["offline_crawl", "offline_crawl_northbound", *MARKET_JOB_IDS]
    northbound = next(j for j in scheduler.jobs if j["id"] == "offline_crawl_northbound")
    assert northbound["args"] == ["northbound"]

    state = _state()
    assert state["northbound_cron"] == "5 15 * * 1-5"
    assert state["northbound_next_run_at"]


def test_market_collectors_get_independent_post_close_crons(fake_daemon):
    assert daemon.run_daemon(force=True, run_now=False, task="all") == 0

    scheduler = _FakeScheduler.instances[-1]
    args = {job["id"]: job["args"] for job in scheduler.jobs}
    assert args["offline_crawl_financials"] == ["financials"]
    assert args["offline_crawl_financials_season"] == ["financials"]
    assert args["offline_crawl_kline"] == ["kline"]
    assert args["offline_crawl_quote"] == ["quote"]

    state = _state()
    assert state["financials_cron"] == "0 18 * * 6"
    assert state["financials_season_cron"] == "0 18 * 1,4,7,8,10 1-5"
    assert state["kline_cron"] == "15 15 * * 1-5"
    assert state["quote_cron"] == "10 15 * * 1-5"
    for key in ("financials", "financials_season", "kline", "quote"):
        assert state[f"{key}_next_run_at"]


def test_a_single_non_northbound_task_does_not_schedule_it(fake_daemon):
    assert daemon.run_daemon(force=True, run_now=False, task="announcements") == 0

    scheduler = _FakeScheduler.instances[-1]
    assert scheduler.job_ids() == ["offline_crawl"]
    state = _state()
    assert state["northbound_cron"] == ""
    for key in ("financials", "financials_season", "kline", "quote"):
        assert state[f"{key}_cron"] == ""


def test_a_single_market_task_does_not_duplicate_its_own_cron(fake_daemon):
    assert daemon.run_daemon(force=True, run_now=False, task="kline") == 0

    scheduler = _FakeScheduler.instances[-1]
    assert scheduler.job_ids() == ["offline_crawl"]
    assert _state()["kline_cron"] == ""


def test_event_tick_only_runs_with_publishing_on(fake_daemon, monkeypatch):
    from tickhub_cn.core.config import reload_settings

    assert daemon.run_daemon(force=True, run_now=False, task="all") == 0
    assert "event_tick" not in _FakeScheduler.instances[-1].job_ids()

    monkeypatch.setenv("EVENT_PUBLISH_ENABLE", "true")
    reload_settings()
    assert daemon.run_daemon(force=True, run_now=False, task="all") == 0
    scheduler = _FakeScheduler.instances[-1]
    assert scheduler.job_ids() == ["offline_crawl", "offline_crawl_northbound", *MARKET_JOB_IDS, "event_tick"]
    tick = next(job for job in scheduler.jobs if job["id"] == "event_tick")
    assert tick["trigger"] == "interval"
    assert tick["minutes"] == 5


def test_invalid_northbound_cron_is_reported(fake_daemon, monkeypatch):
    from tickhub_cn.core.config import reload_settings

    monkeypatch.setenv("OFFLINE_CRAWLER_NORTHBOUND_CRON", "not a cron")
    reload_settings()

    assert daemon.run_daemon(force=True, run_now=False, task="all") == 3
    assert _FakeScheduler.instances == []


@pytest.mark.parametrize(
    "env_name",
    [
        "OFFLINE_CRAWLER_FINANCIALS_CRON",
        "OFFLINE_CRAWLER_FINANCIALS_SEASON_CRON",
        "OFFLINE_CRAWLER_KLINE_CRON",
        "OFFLINE_CRAWLER_QUOTE_CRON",
    ],
)
def test_invalid_market_cron_is_reported(fake_daemon, monkeypatch, env_name):
    from tickhub_cn.core.config import reload_settings

    monkeypatch.setenv(env_name, "not a cron")
    reload_settings()

    assert daemon.run_daemon(force=True, run_now=False, task="all") == 3
    assert _FakeScheduler.instances == []
