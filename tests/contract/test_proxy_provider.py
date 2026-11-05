"""Config-driven exit provider: switching pool / commercial / direct is config-only."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tickhub_cn.proxy import exit as exit_mod
from tickhub_cn.proxy.pool import load_entries


class _FakeLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def info(self, message: str, extra: dict | None = None) -> None:
        self.records.append((message, extra or {}))


class _EmptyPool:
    nodes: list = []

    def acquire(self, exclude=frozenset()):
        return None


@pytest.fixture
def fake_log(monkeypatch):
    log = _FakeLog()
    monkeypatch.setattr(exit_mod, "_log", log)
    monkeypatch.setattr(exit_mod, "_last_selection", "")
    monkeypatch.setattr("tickhub_cn.proxy.pool.get_pool", lambda: _EmptyPool())
    return log


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        proxy_provider="",
        proxy_pool_enable=False,
        proxy_pool_url="",
        proxy_pool_config_path="./does-not-exist.json",
        proxy_pool_list="",
        proxy_commercial_url="",
        proxy_commercial_username="",
        proxy_commercial_password="",
        proxy_commercial_whitelist="",
        proxy_url="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _use(monkeypatch, settings: SimpleNamespace) -> None:
    monkeypatch.setattr(exit_mod, "get_settings", lambda: settings)


def test_blank_provider_follows_legacy_flag():
    assert exit_mod.resolve_provider(_settings(proxy_pool_enable=False)) == "direct"
    assert exit_mod.resolve_provider(_settings(proxy_pool_enable=True)) == "pool"


def test_direct_returns_no_proxy(monkeypatch, fake_log):
    _use(monkeypatch, _settings(proxy_provider="direct", proxy_pool_url="http://127.0.0.1:8789"))

    proxies, node = exit_mod.select_exit()

    assert proxies is None and node is None
    assert fake_log.records[-1][1]["detail"]["provider"] == "direct"


def test_pool_uses_the_configured_gateway(monkeypatch, fake_log):
    _use(monkeypatch, _settings(proxy_provider="pool", proxy_pool_url="http://127.0.0.1:8789"))

    proxies, node = exit_mod.select_exit()

    assert node is None
    assert proxies == {"http": "http://127.0.0.1:8789", "https": "http://127.0.0.1:8789"}
    assert fake_log.records[-1][1]["detail"]["provider"] == "pool"


def test_commercial_uses_the_configured_url(monkeypatch, fake_log):
    _use(
        monkeypatch,
        _settings(proxy_provider="commercial", proxy_commercial_url="http://gate.example:8000"),
    )

    proxies, node = exit_mod.select_exit()

    assert node is None
    assert proxies == {"http": "http://gate.example:8000", "https": "http://gate.example:8000"}
    detail = fake_log.records[-1][1]["detail"]
    assert detail["provider"] == "commercial"
    assert detail["auth"] == "none"


def test_commercial_embeds_basic_auth_without_logging_it(monkeypatch, fake_log):
    _use(
        monkeypatch,
        _settings(
            proxy_provider="commercial",
            proxy_commercial_url="http://gate.example:8000",
            proxy_commercial_username="user@corp",
            proxy_commercial_password="p:ss",
        ),
    )

    proxies, _ = exit_mod.select_exit()

    assert proxies["https"] == "http://user%40corp:p%3Ass@gate.example:8000"
    rendered = json.dumps(fake_log.records, ensure_ascii=False)
    assert "user@corp" not in rendered and "p:ss" not in rendered


def test_commercial_whitelist_is_config_only(monkeypatch, fake_log):
    _use(
        monkeypatch,
        _settings(
            proxy_provider="commercial",
            proxy_commercial_url="http://gate.example:8000",
            proxy_commercial_whitelist="10.0.0.1, 10.0.0.2",
        ),
    )

    proxies, _ = exit_mod.select_exit()

    assert "@" not in proxies["http"]
    detail = fake_log.records[-1][1]["detail"]
    assert detail["auth"] == "whitelist"
    assert detail["whitelist_entries"] == 2


def test_unconfigured_commercial_falls_back_to_pool(monkeypatch, fake_log):
    _use(
        monkeypatch,
        _settings(
            proxy_provider="commercial",
            proxy_commercial_url="",
            proxy_pool_url="http://127.0.0.1:8789",
        ),
    )

    proxies, _ = exit_mod.select_exit()

    assert proxies == {"http": "http://127.0.0.1:8789", "https": "http://127.0.0.1:8789"}
    details = [record[1]["detail"] for record in fake_log.records]
    assert any(d.get("provider") == "commercial" and d.get("fallback") == "pool" for d in details)


def test_provider_selection_is_logged_once_per_configuration(monkeypatch, fake_log):
    _use(monkeypatch, _settings(proxy_provider="pool", proxy_pool_url="http://127.0.0.1:8789"))

    exit_mod.select_exit()
    exit_mod.select_exit()

    assert len(fake_log.records) == 1


def test_pool_url_overrides_commercial_entries_left_in_the_file(tmp_path):
    path = tmp_path / "proxy_pool.json"
    path.write_text(json.dumps({"proxies": [{"url": "http://old-commercial:15818"}]}), encoding="utf-8")
    settings = _settings(proxy_pool_url="http://127.0.0.1:8789", proxy_pool_config_path=str(path))

    entries = load_entries(settings)

    assert [entry["url"] for entry in entries] == ["http://127.0.0.1:8789"]


def test_file_entries_still_load_when_pool_url_is_blank(tmp_path):
    path = tmp_path / "proxy_pool.json"
    path.write_text(json.dumps({"proxies": [{"url": "http://127.0.0.1:1"}]}), encoding="utf-8")
    settings = _settings(proxy_pool_config_path=str(path))

    entries = load_entries(settings)

    assert [entry["url"] for entry in entries] == ["http://127.0.0.1:1"]
