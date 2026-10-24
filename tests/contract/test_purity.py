"""CT-08: importing the package opens no socket, starts no thread, builds no settings."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROBE = r"""
import json
import sys
import threading

NET_EVENTS = {"socket.__new__", "socket.connect", "socket.bind", "socket.getaddrinfo"}


def _guard(event, args):
    if event in NET_EVENTS:
        raise RuntimeError(f"network activity at import: {event}")


sys.addaudithook(_guard)

import tickhub_cn  # noqa: E402
from tickhub_cn.core.config import get_settings  # noqa: E402

assert get_settings.cache_info().currsize == 0, "importing tickhub_cn built Settings"

import tickhub_cn.datasources  # noqa: E402,F401
from tickhub_cn.datasources.base import ALIASES, REGISTRY  # noqa: E402

assert get_settings.cache_info().currsize == 0, "importing datasources built Settings"

import tickhub_cn.admin_api.app  # noqa: E402,F401
import tickhub_cn.cli.main  # noqa: E402,F401
import tickhub_cn.cli.start_all  # noqa: E402,F401
import tickhub_cn.crawler.daemon  # noqa: E402,F401
import tickhub_cn.mcp.server  # noqa: E402,F401

extra = [t.name for t in threading.enumerate() if t is not threading.main_thread()]
assert not extra, f"threads started at import: {extra}"
print(json.dumps({"tools": len(REGISTRY), "aliases": len(ALIASES)}))
"""


def test_import_is_side_effect_free(repo_root: Path, tmp_path: Path) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": str(repo_root / "src"),
        "PYTHONIOENCODING": "utf-8",
        "REDIS_URL": "redis://127.0.0.1:6399/0",
    }
    before = {p.name for p in tmp_path.iterdir()}
    proc = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {"tools": 119, "aliases": 25}
    assert {p.name for p in tmp_path.iterdir()} == before, "import wrote into the working directory"
