"""``th-cn start-all``: one command, two OS processes, one port.

Layout
------
main process  Streamable HTTP MCP server on ``--port`` (default 8000) and
              supervisor for the crawler child.
child process Blocking cron crawler. No socket, no port.

The supervisor restarts the child when it crashes, stops restarting after
``restart_limit`` crashes in five minutes (severe alert, MCP keeps serving),
and turns SIGINT/SIGTERM into a graceful child shutdown.
"""

from __future__ import annotations

import collections
import multiprocessing as mp
import signal
import socket
import threading
import time
from typing import Any

from ..core.config import get_settings
from ..logging.setup import get_logger

CRASH_WINDOW_S = 300
PORT_READY_TIMEOUT_S = 20.0


def _crawler_process(task: str, force: bool, stop_event: Any) -> None:
    """Child entry point (must be importable under the spawn start method)."""
    from ..core.config import get_settings as _settings
    from ..crawler import daemon
    from ..logging.setup import setup_logging

    setup_logging(_settings(), force=True)

    def watch() -> None:
        stop_event.wait()
        daemon.request_stop()

    threading.Thread(target=watch, name="stop-watch", daemon=True).start()
    raise SystemExit(daemon.run_daemon(force=force, task=task))


def _http_thread(host: str, port: int, holder: dict[str, Any]) -> None:
    import uvicorn

    from ..mcp.server import create_app

    try:
        app = create_app(host=host)
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=get_settings().log_level.lower(),
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config)
        holder["server"] = server
        server.run()
    except BaseException as exc:  # noqa: BLE001 - reported by the supervisor
        holder["error"] = repr(exc)


def _wait_port(host: str, port: int, timeout: float = PORT_READY_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((target, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _install_signals(stop: threading.Event) -> None:
    def handler(signum: int, _frame: Any) -> None:
        if stop.is_set():
            return
        get_logger("startall").info(
            "shutting down", extra={"event": "startall_stop", "detail": {"signal": signum}}
        )
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, AttributeError):
            pass


def _spawn(ctx: Any, task: str, force: bool, stop_event: Any) -> Any:
    proc = ctx.Process(
        target=_crawler_process,
        args=(task, force, stop_event),
        name="tickhub-crawler",
        daemon=True,
    )
    proc.start()
    get_logger("startall").info(
        "crawler child started",
        extra={"event": "crawler_child_start", "detail": {"pid": proc.pid, "task": task}},
    )
    return proc


def _stop_child(proc: Any, stop_event: Any) -> None:
    if proc is None:
        return
    stop_event.set()
    proc.join(timeout=10)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=5)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=5)


def start_all(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    task: str = "all",
    crawler: str = "auto",
    restart_limit: int = 3,
) -> int:
    """Run the MCP HTTP server and supervise the crawler child. Blocks."""
    settings = get_settings()
    log = get_logger("startall")

    crawler_mode = (crawler or "auto").lower()
    if crawler_mode not in ("auto", "on", "off"):
        log.error(
            "invalid --crawler value",
            extra={"error_code": "QFSD-9001", "detail": {"value": crawler}},
        )
        return 2
    run_crawler = crawler_mode == "on" or (crawler_mode == "auto" and settings.offline_crawler_enable)
    force_crawler = crawler_mode == "on" and not settings.offline_crawler_enable
    if crawler_mode == "auto" and not settings.offline_crawler_enable:
        log.warning(
            "offline crawler disabled; serving MCP only",
            extra={
                "event": "crawler_disabled",
                "detail": {"hint": "set OFFLINE_CRAWLER_ENABLE=true or pass --crawler on"},
            },
        )

    ctx = mp.get_context("spawn")
    stop_event = ctx.Event()
    holder: dict[str, Any] = {}
    thread = threading.Thread(target=_http_thread, args=(host, port, holder), name="mcp-http", daemon=True)
    thread.start()

    if not _wait_port(host, port):
        log.error(
            "MCP HTTP server failed to start",
            extra={"error_code": "QFSD-9003", "detail": {"host": host, "port": port, "error": holder.get("error")}},
        )
        _stop_child(None, stop_event)
        return 3

    log.info(
        "start-all ready",
        extra={
            "event": "startall_ready",
            "detail": {"mcp": f"http://{host}:{port}/mcp", "crawler": run_crawler, "pid": __import__("os").getpid()},
        },
    )

    child = _spawn(ctx, task, force_crawler, stop_event) if run_crawler else None
    _install_signals(stop_event)
    crashes: collections.deque[float] = collections.deque()
    degraded = False

    try:
        while not stop_event.wait(1.0):
            if child is None or child.is_alive():
                continue
            code = child.exitcode
            child.close()
            child = None
            if code == 0:
                log.warning(
                    "crawler child exited cleanly; supervisor idle",
                    extra={"event": "crawler_child_exit", "detail": {"exitcode": 0}},
                )
                continue
            now = time.monotonic()
            while crashes and now - crashes[0] > CRASH_WINDOW_S:
                crashes.popleft()
            crashes.append(now)
            if len(crashes) > max(1, int(restart_limit)):
                if not degraded:
                    degraded = True
                    log.error(
                        "crawler child crashed repeatedly; supervisor degraded (MCP still serving)",
                        extra={
                            "event": "crawler_supervisor_degraded",
                            "error_code": "QFSD-9003",
                            "detail": {"crashes": len(crashes), "window_s": CRASH_WINDOW_S},
                        },
                    )
                continue
            log.warning(
                "crawler child exited; restarting",
                extra={
                    "event": "crawler_child_restart",
                    "detail": {"exitcode": code, "restart": len(crashes), "window_s": CRASH_WINDOW_S},
                },
            )
            child = _spawn(ctx, task, force_crawler, stop_event)
    finally:
        _stop_child(child, stop_event)
        server = holder.get("server")
        if server is not None:
            server.should_exit = True
        thread.join(timeout=10)
        try:
            from ..proxy.pool import reset_pool

            reset_pool()
        except Exception:  # noqa: BLE001
            pass
        log.info("start-all stopped", extra={"event": "startall_stopped"})
    return 0


__all__ = ["start_all"]
