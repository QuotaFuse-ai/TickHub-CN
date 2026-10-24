"""Process helpers for the integration suite.

Every server test spawns the real CLI with ``python -m tickhub_cn`` in its own
working directory, so stdout/stderr separation, process layout and logging are
exercised exactly as an operator would see them.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def child_env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH", "")]))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.update(overrides)
    return env


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_open(port: int, *, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(port):
            return
        time.sleep(0.1)
    raise AssertionError(f"port {port} did not open within {timeout}s")


def spawn(args: list[str], *, cwd: Path, log: Path | None = None, **env: str) -> subprocess.Popen:
    """Start the CLI with stdout/stderr captured (file by default, to avoid pipe stalls)."""
    stdout: Any = subprocess.DEVNULL
    stderr: Any = subprocess.DEVNULL
    handle = None
    if log is not None:
        handle = open(log, "ab", buffering=0)
        stdout = handle
        stderr = subprocess.STDOUT
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, "-m", "tickhub_cn", *args],
        cwd=cwd,
        env=child_env(**env),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        **kwargs,
    )
    if handle is not None:
        # The child owns the descriptor; the parent copy is no longer needed.
        handle.close()
    return proc


def kill_pid(pid: int) -> None:
    """Terminate exactly one pid. Never used with a process name."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
        ).stdout
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 2 and cols[1] == str(pid):
                return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class JsonLog:
    """Incremental reader over a JSON-per-line log file."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.rows: list[dict[str, Any]] = []
        self.bad_lines: list[str] = []

    def _read_new(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open("rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()
        for raw in chunk.splitlines():
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                self.bad_lines.append(text)
                continue
            # Wrapped Rich output can leave a bare number on its own line.
            if isinstance(row, dict):
                self.rows.append(row)
            else:
                self.bad_lines.append(text)
        return self.rows

    def find(self, event: str, *, timeout: float = 30.0, start: int = 0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for row in self._read_new()[start:]:
                if row.get("event") == event:
                    return row
            time.sleep(0.1)
        raise AssertionError(f"log event {event!r} not seen within {timeout}s in {self.path}")

    def events(self) -> list[str]:
        self._read_new()
        return [str(row.get("event")) for row in self.rows if row.get("event")]
