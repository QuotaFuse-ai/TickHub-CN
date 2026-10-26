"""Shared pytest setup for TickHub-CN.

Every test runs inside its own temporary working directory with the Redis
backend disabled (in-process cache) and the archive pointed at ``tmp_path``.
That keeps ``./logs/``, ``./var/archive`` and ``field_map.toml`` writes out of
the repository, so tests never touch a developer's real archive or cache.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for entry in (str(SRC), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

# Console encoding: CLI tests compare captured output against UTF-8 text.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


@pytest.fixture(autouse=True)
def isolated_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """chdir into a scratch dir and point every writable setting at it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    shutil.copy(ROOT / "field_map.toml", tmp_path / "field_map.toml")

    # An unroutable port keeps the suite on the in-process store: an empty value
    # would fall back to the shipped default (settings use ``env or default``).
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/0")
    monkeypatch.setenv("FIELD_MAP_PATH", str(tmp_path / "field_map.toml"))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "var" / "archive"))
    monkeypatch.setenv("ARCHIVE_DB_PATH", str(tmp_path / "var" / "archive" / "tickhub.db"))
    monkeypatch.setenv("LOG_LEVEL", "critical")
    monkeypatch.setenv("LOG_SINK", "stderr")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "tickhub.log"))
    monkeypatch.setenv("CACHE_ENV", "test")
    monkeypatch.setenv("HTTP_TIMEOUT", "5")
    monkeypatch.setenv("HTTP_RETRIES", "0")
    monkeypatch.setenv("PROXY_URL", "")
    monkeypatch.setenv("ADMIN_ENABLE", "0")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "")
    monkeypatch.setenv("ADMIN_READONLY", "0")

    from tickhub_cn.cache.redis_cache import reset_cache
    from tickhub_cn.core.config import reload_settings
    from tickhub_cn.crawler.store import reset_archive
    from tickhub_cn.fieldmap.loader import reload_field_map
    from tickhub_cn.model_gateway import reload_gateway_config

    reload_settings()
    reset_cache()
    reset_archive()
    reload_field_map()
    reload_gateway_config()
    try:
        yield tmp_path
    finally:
        reset_cache()
        reset_archive()
        reload_settings()
        reload_field_map()
        reload_gateway_config()


@pytest.fixture
def repo_root() -> Path:
    return ROOT


@pytest.fixture
def run_cli():
    """Invoke the Typer app in-process and return (exit_code, stdout, stderr)."""
    from typer.testing import CliRunner

    from tickhub_cn.cli.main import app

    def _run(*args: str, catch_exceptions: bool = True):
        result = CliRunner().invoke(app, list(args), catch_exceptions=catch_exceptions)
        return result.exit_code, result.stdout, result.stderr

    return _run


@pytest.fixture
def run_cli_json(run_cli):
    """Like ``run_cli`` but parses stdout as a JSON document."""

    def _run(*args: str) -> tuple[int, dict[str, Any], str]:
        code, stdout, stderr = run_cli(*args)
        payload = json.loads(stdout)
        return code, payload, stderr

    return _run
