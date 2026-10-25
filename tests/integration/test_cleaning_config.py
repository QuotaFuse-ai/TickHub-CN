"""Text-cleaning switch (spec §4.11 / §10.5).

The switch lives in ``model_gateway.toml`` and is edited from the admin
console. Cleaning is off by default and, when it is off or the gateway is not
ready, ``clean_text`` must fall back to local tag stripping with no model call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tickhub_cn.admin_api.app import cleaning_state, create_app
from tickhub_cn.core.config import get_settings, reload_settings

TOKEN = "cleaning-token"
CONFIG = Path("model_gateway.toml")


def _client(**overrides: Any) -> Any:
    from fastapi.testclient import TestClient

    admin = {"auth_token": TOKEN, "readonly": False, "audit_sink": "file", "sensitive_mask": True}
    admin.update(overrides)
    return TestClient(create_app(get_settings(), admin=admin))


def _headers() -> dict[str, str]:
    return {"x-admin-token": TOKEN}


def _audit() -> list[dict[str, Any]]:
    path = Path("./logs/admin_audit.jsonl")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _toml() -> dict[str, Any]:
    import toml

    return toml.loads(CONFIG.read_text(encoding="utf-8"))["model_gateway"]


def test_cleaning_is_off_by_default() -> None:
    state = _client().get("/api/config/cleaning", headers=_headers()).json()["cleaning"]
    assert state["enable"] is False
    assert state["effective"] is False
    assert state["ready"] is False
    assert state["api_key"] == ""
    assert state["error"] is None
    assert state["legacy_enable"] is False
    assert state["env_locked"] == {"enable": False, "base_url": False, "api_key": False, "model": False}
    assert state["prompt"]  # the built-in prompt is still shown as the default
    assert not CONFIG.exists()

    from tickhub_cn.crawler.cleaner import cleaning_enabled

    assert cleaning_enabled() is False


def test_cleaning_save_writes_toml_and_takes_effect() -> None:
    saved = _client().put(
        "/api/config/cleaning",
        headers=_headers(),
        json={
            "enable": True,
            "base_url": "https://gateway.example/v1",
            "model": "qwen-plus",
            "api_key": "sk-live-secret",
            "prompt": "只保留公告正文",
        },
    )
    assert saved.status_code == 200, saved.text
    state = saved.json()["cleaning"]
    assert state["enable"] is True
    assert state["effective"] is True
    assert state["ready"] is True
    assert state["model"] == "qwen-plus"
    assert state["prompt"] == "只保留公告正文"
    assert state["prompt_from_file"] is True
    assert state["api_key"] == "***"
    assert "sk-live-secret" not in saved.text

    text = CONFIG.read_text(encoding="utf-8")
    assert text.startswith("# Optional multi-model gateway.")
    assert "NEVER routes prices" in text
    section = _toml()
    assert section["enable"] is True
    assert section["base_url"] == "https://gateway.example/v1"
    assert section["routing"]["text_clean"] == "qwen-plus"
    assert section["cleaning"]["prompt"] == "只保留公告正文"
    assert section["api_key"] == "sk-live-secret"

    # The MCP process reads the same file; a fresh read must already see it.
    from tickhub_cn.model_gateway import get_gateway_config

    assert get_gateway_config().model_for("text_clean") == "qwen-plus"

    audit = Path("./logs/admin_audit.jsonl").read_text(encoding="utf-8")
    assert "config.cleaning" in audit
    assert "sk-live-secret" not in audit

    listed = _client().get("/api/config/cleaning", headers=_headers()).json()["cleaning"]
    assert listed["enable"] is True
    assert listed["model"] == "qwen-plus"


def test_cleaning_save_keeps_stored_key_and_makes_a_backup() -> None:
    client = _client()
    assert client.put(
        "/api/config/cleaning",
        headers=_headers(),
        json={"enable": True, "base_url": "https://gateway.example/v1", "model": "m1", "api_key": "sk-keep"},
    ).status_code == 200

    second = client.put(
        "/api/config/cleaning",
        headers=_headers(),
        json={"enable": False, "base_url": "https://gateway.example/v1", "model": "m2", "api_key": "***"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["cleaning"]["effective"] is False
    section = _toml()
    assert section["api_key"] == "sk-keep"
    assert section["enable"] is False
    assert section["routing"]["text_clean"] == "m2"

    backup = CONFIG.with_suffix(CONFIG.suffix + ".bak")
    assert backup.exists()
    assert '"sk-keep"' in backup.read_text(encoding="utf-8")


def test_cleaning_save_validates_and_respects_readonly() -> None:
    client = _client()
    bad_scheme = client.put(
        "/api/config/cleaning", headers=_headers(), json={"enable": False, "base_url": "api.example.com/v1"}
    )
    assert bad_scheme.status_code == 422
    assert "http://" in bad_scheme.json()["detail"]

    no_base = client.put("/api/config/cleaning", headers=_headers(), json={"enable": True, "base_url": ""})
    assert no_base.status_code == 422
    assert "base_url is required" in no_base.json()["detail"]

    no_model = client.put(
        "/api/config/cleaning", headers=_headers(), json={"enable": True, "base_url": "https://gw.example/v1"}
    )
    assert no_model.status_code == 422
    assert "model is required" in no_model.json()["detail"]
    assert not CONFIG.exists()

    forbidden = _client(readonly=True).put("/api/config/cleaning", headers=_headers(), json={"enable": False})
    assert forbidden.status_code == 403


def test_cleaning_test_strips_tags_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    html = "<div><h1>公告</h1><p>2025 年净利润 1.2 亿元</p><script>ads()</script></div>"

    # Switch off: the endpoint is honest about doing nothing.
    off = client.post("/api/config/cleaning/test", headers=_headers(), json={"text": html})
    assert off.status_code == 200, off.text
    assert off.json()["cleaning"]["ready"] is False
    assert off.json()["result"]["clean_content"] == html

    # Enabled but no gateway: local tag stripping, still no model call.
    monkeypatch.setenv("CRAWL4AI_ENABLE", "true")
    reload_settings()
    from tickhub_cn.model_gateway import reload_gateway_config

    reload_gateway_config()
    reply = client.post("/api/config/cleaning/test", headers=_headers(), json={"text": html})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["cleaning"]["ready"] is False
    result = body["result"]
    assert result["raw_content"] == html
    assert "<div>" not in result["clean_content"]
    assert "公告" in result["clean_content"]
    assert "ads()" not in result["clean_content"]
    assert "1.2 亿元" in result["clean_content"]  # numbers are never rewritten
    assert result["stat"]["raw_len"] > result["stat"]["clean_len"] > 0
    assert 0 < result["stat"]["reduce_rate"] < 1

    assert client.post("/api/config/cleaning/test", headers=_headers(), json={"text": "   "}).status_code == 422
    read_only = _client(readonly=True).post("/api/config/cleaning/test", headers=_headers(), json={"text": "纯文本"})
    assert read_only.status_code == 200

    assert "config.cleaning_test" in {row["action"] for row in _audit()}


def test_gateway_env_wins_and_crawl4ai_model_only_overrides_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    CONFIG.write_text(
        "[model_gateway]\n"
        'enable = true\n'
        'base_url = "https://file.example/v1"\n'
        'api_key = "file-key"\n'
        'default_model = "file-default"\n'
        "[model_gateway.routing]\n"
        'text_clean = "file-clean"\n',
        encoding="utf-8",
    )
    from tickhub_cn.model_gateway import get_gateway_config, reload_gateway_config

    reload_settings()
    conf = reload_gateway_config()
    assert conf.base_url == "https://file.example/v1"
    assert conf.model_for("text_clean") == "file-clean"
    assert conf.prompt == ""

    monkeypatch.setenv("MODEL_GATEWAY_ENABLE", "true")
    monkeypatch.setenv("MODEL_GATEWAY_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "env-key")
    monkeypatch.setenv("MODEL_GATEWAY_DEFAULT_MODEL", "env-default")
    monkeypatch.setenv("CRAWL4AI_MODEL", "env-clean")
    reload_settings()
    conf = reload_gateway_config()
    assert conf.enable is True
    assert conf.base_url == "https://env.example/v1"
    assert conf.api_key == "env-key"
    assert conf.default_model == "env-default"
    assert conf.model_for("text_clean") == "env-clean"
    assert get_gateway_config() is conf  # cached until the file or env changes


def test_gateway_refreshes_when_the_file_changes() -> None:
    from tickhub_cn.model_gateway import get_gateway_config, reload_gateway_config

    reload_gateway_config()
    CONFIG.write_text(
        '[model_gateway]\nenable = true\nbase_url = "https://one.example/v1"\n', encoding="utf-8"
    )
    assert get_gateway_config().base_url == "https://one.example/v1"

    # No explicit reload: the admin process saves from another process, so the
    # cache must notice the file changed on its own.
    CONFIG.write_text(
        '[model_gateway]\nenable = false\nbase_url = "https://second.example/v1"\n', encoding="utf-8"
    )
    conf = get_gateway_config()
    assert conf.base_url == "https://second.example/v1"
    assert conf.enable is False


def test_malformed_gateway_file_reports_an_error() -> None:
    CONFIG.write_text("[model_gateway\n", encoding="utf-8")
    state = cleaning_state()
    assert state["error"]
    assert state["enable"] is False
    assert state["effective"] is False


def test_legacy_crawl4ai_enable_still_switches_cleaning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWL4AI_ENABLE", "true")
    reload_settings()
    from tickhub_cn.crawler.cleaner import cleaning_enabled
    from tickhub_cn.model_gateway import reload_gateway_config

    reload_gateway_config()
    assert cleaning_enabled() is True
    state = cleaning_state()
    assert state["legacy_enable"] is True
    assert state["enable"] is False  # the file switch itself is still off
    assert state["effective"] is True
    assert state["env_locked"]["enable"] is True
    assert state["ready"] is False  # no base_url -> no model call
