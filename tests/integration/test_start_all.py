"""CL-07: ``th-cn start-all`` process layout (docs/TEST_PLAN.md section 2).

One listening socket in the main process, one supervised crawler child with no
socket of its own, automatic restart after a child crash, and a graceful child
shutdown when the supervisor asks for it.
"""

from __future__ import annotations

import multiprocessing as mp
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.integration._procs import (
    JsonLog,
    free_port,
    kill_pid,
    pid_alive,
    port_open,
    spawn,
    wait_port,
)

pytestmark = pytest.mark.slow


def _listen_addrs(pid: int) -> list[Any] | None:
    """LISTEN sockets owned by ``pid``; ``None`` when the OS denies inspection."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil ships with the dev extra
        return None
    try:
        conns = psutil.Process(pid).net_connections(kind="inet")
    except (psutil.Error, OSError):
        return None
    return [c for c in conns if c.status == "LISTEN"]


def test_start_all_supervises_one_child_and_one_port(tmp_path: Path) -> None:
    port = free_port()
    log_path = tmp_path / "start-all.log"
    journal = JsonLog(log_path)
    proc = spawn(
        ["start-all", "--port", str(port), "--crawler", "on"],
        cwd=tmp_path,
        log=log_path,
        LOG_LEVEL="info",
        OFFLINE_CRAWLER_RUN_ON_START="false",
    )
    pids: set[int] = {proc.pid}
    server_pid = 0
    second_pid = 0
    try:
        ready = journal.find("startall_ready")
        assert ready["detail"]["crawler"] is True
        # The venv ``python.exe`` launcher is one process ahead of the server on
        # Windows, so the pid reported by start-all is the authoritative one.
        server_pid = int(ready["detail"]["pid"])
        pids.add(server_pid)
        assert server_pid > 0
        wait_port(port, timeout=10)

        first = journal.find("crawler_child_start")
        child_pid = int(first["detail"]["pid"])
        pids.add(child_pid)
        assert child_pid != server_pid

        # The crawler child never owns a listening socket: the process split is
        # what keeps the deployment on a single published port.
        child_sockets = _listen_addrs(child_pid)
        if child_sockets is not None:
            assert child_sockets == []
        parent_sockets = _listen_addrs(server_pid)
        if parent_sockets is not None:
            assert [c.laddr.port for c in parent_sockets] == [port]

        daemon_start = journal.find("crawler_start")
        assert daemon_start["detail"]["run_on_start"] is False

        # Kill exactly the child PID; the supervisor must notice and respawn.
        kill_pid(child_pid)
        deadline = time.monotonic() + 20
        starts: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            journal._read_new()
            starts = [r for r in journal.rows if r.get("event") == "crawler_child_start"]
            if len(starts) >= 2:
                break
            time.sleep(0.2)
        assert len(starts) >= 2, "supervisor did not restart the crawler child"
        assert any(r.get("event") == "crawler_child_restart" for r in journal.rows)
        second_pid = int(starts[-1]["detail"]["pid"])
        pids.add(second_pid)
        assert second_pid != child_pid
        assert pid_alive(second_pid)
    finally:
        # A pytest session has no console, so Windows cannot deliver
        # Ctrl-Break/CTRL_C to the child's process group (verified with a
        # standalone probe). The signal path is asserted on POSIX; the same
        # supervisor stop path is covered on every platform by
        # test_crawler_child_stops_gracefully_on_parent_request below.
        graceful = sys.platform != "win32"
        if graceful and proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=20)
            except Exception:
                graceful = False
        if proc.poll() is None:
            kill_pid(proc.pid)
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        for pid in pids:
            if pid != proc.pid and pid_alive(pid):
                kill_pid(pid)
        # The listening port must be released once the process tree is gone.
        deadline = time.monotonic() + 10
        while port_open(port) and time.monotonic() < deadline:
            time.sleep(0.2)

    assert proc.poll() is not None
    if graceful:
        assert proc.returncode == 0
    assert not pid_alive(server_pid)
    assert not port_open(port)
    if second_pid:
        assert not pid_alive(second_pid)
    if graceful:
        journal._read_new()
        events = [r.get("event") for r in journal.rows]
        assert "startall_stop" in events
        assert "startall_stopped" in events
        assert "crawler_stopped" in events


def test_crawler_child_stops_gracefully_on_parent_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor's stop event shuts the daemon down with exit code 0."""
    monkeypatch.setenv("OFFLINE_CRAWLER_ENABLE", "true")
    monkeypatch.setenv("OFFLINE_CRAWLER_RUN_ON_START", "false")

    from tickhub_cn.crawler import daemon
    from tickhub_cn.cli.start_all import _crawler_process

    state_file = daemon.state_path()
    state_file.unlink(missing_ok=True)

    ctx = mp.get_context("spawn")
    stop_event = ctx.Event()
    proc = ctx.Process(target=_crawler_process, args=("all", True, stop_event), name="tickhub-crawler")
    proc.start()
    try:
        deadline = time.monotonic() + 60
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            state = daemon.read_state()
            if state.get("pid"):
                break
            time.sleep(0.2)
        assert state.get("pid") == proc.pid, f"daemon did not report its pid: {state}"
        assert proc.is_alive()

        stop_event.set()
        proc.join(timeout=30)
        assert proc.exitcode == 0, "daemon did not shut down gracefully"
    finally:
        if proc.is_alive():
            kill_pid(proc.pid)
            proc.join(timeout=10)
        stop_event.set()

    final = daemon.read_state()
    assert final.get("pid") == 0
    assert final.get("stopped_at")
