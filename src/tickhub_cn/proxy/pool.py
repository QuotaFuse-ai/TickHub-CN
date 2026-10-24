"""Authenticated proxy pool with health checks and direct-connect fallback."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

from ..core.config import get_settings
from ..logging.setup import get_logger

MASK = "***"


def _mask(value: str) -> str:
    if not value:
        return ""
    return value[:2] + MASK if len(value) > 4 else MASK


@dataclass
class ProxyNode:
    raw: str
    username: str = ""
    password: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    healthy: bool = True
    failures: int = 0
    latency_ms: float | None = None
    last_checked: float | None = None

    @property
    def id(self) -> str:
        host = urlparse(self.raw).netloc or self.raw
        return f"{host}|{_mask(self.username)}"

    def requests_url(self) -> str:
        if not self.username:
            return self.raw
        parsed = urlparse(self.raw)
        auth = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        return urlunparse(parsed._replace(netloc=auth + parsed.netloc))

    def as_requests_proxy(self) -> dict[str, str]:
        url = self.requests_url()
        return {"http": url, "https": url}

    def masked(self) -> dict[str, Any]:
        parsed = urlparse(self.raw)
        safe = urlunparse(parsed._replace(netloc=parsed.hostname or ""))
        return {
            "id": self.id,
            "url": safe,
            "username": self.username,
            "has_password": bool(self.password),
            "headers": {k: MASK for k in self.headers},
            "healthy": self.healthy,
            "failures": self.failures,
            "latency_ms": self.latency_ms,
        }


class ProxyPool:
    # A node is dropped only after this many consecutive connection-level
    # failures; a single successful response resets the counter.
    failure_threshold = 3

    def __init__(self, nodes: list[ProxyNode] | None = None) -> None:
        self.nodes: list[ProxyNode] = nodes or []
        self._idx = 0
        self._lock = threading.Lock()
        self._monitor: threading.Thread | None = None
        self._monitor_stop = threading.Event()

    @classmethod
    def from_settings(cls) -> "ProxyPool":
        settings = get_settings()
        if not settings.proxy_pool_enable:
            return cls([])
        entries = load_entries(settings)
        return cls([_to_node(e) for e in entries])

    def __bool__(self) -> bool:
        return bool(self.nodes)

    def acquire(self) -> ProxyNode | None:
        if not self.nodes:
            return None
        with self._lock:
            n = len(self.nodes)
            for _ in range(n):
                node = self.nodes[self._idx % n]
                self._idx += 1
                if node.healthy:
                    return node
        get_logger("proxy").warning("proxy pool exhausted, direct fallback", extra={"error_code": "QFSD-5001"})
        return None

    def mark(self, node: ProxyNode, ok: bool, latency_ms: float | None = None) -> None:
        disabled = recovered = False
        with self._lock:
            node.latency_ms = latency_ms
            node.last_checked = time.time()
            if ok:
                recovered = not node.healthy
                node.failures = 0
                node.healthy = True
            else:
                node.failures += 1
                if node.failures >= self.failure_threshold and node.healthy:
                    node.healthy = False
                    disabled = True
        if disabled:
            get_logger("proxy").warning(
                "proxy node disabled after consecutive failures",
                extra={"error_code": "QFSD-5001", "detail": {"node": node.id, "failures": node.failures}},
            )
        elif recovered:
            get_logger("proxy").info("proxy node recovered", extra={"detail": {"node": node.id}})

    def check(self, node: ProxyNode, timeout: int | None = None) -> bool:
        import requests

        settings = get_settings()
        timeout = timeout or settings.proxy_pool_check_timeout
        started = time.monotonic()
        try:
            resp = requests.get(
                "https://www.baidu.com/",
                proxies=node.as_requests_proxy(),
                headers={**node.headers, "User-Agent": "Mozilla/5.0"},
                timeout=timeout,
            )
            ok = resp.status_code < 500
        except Exception:
            ok = False
        self.mark(node, ok, (time.monotonic() - started) * 1000)
        return ok

    def check_all(self, timeout: int | None = None) -> list[dict[str, Any]]:
        for node in self.nodes:
            self.check(node, timeout)
        return [n.masked() for n in self.nodes]

    def start_monitor(self, interval: float | None = None) -> bool:
        """Start the interval health checker. Returns False when already running."""
        if not self.nodes:
            return False
        period = float(get_settings().proxy_pool_check_interval if interval is None else interval)
        if period <= 0:
            period = 60.0
        with self._lock:
            if self._monitor is not None and self._monitor.is_alive():
                return False
            self._monitor_stop.clear()
            self._monitor = threading.Thread(
                target=self._monitor_loop,
                args=(period,),
                name="proxy-pool-monitor",
                daemon=True,
            )
            self._monitor.start()
        return True

    def _monitor_loop(self, period: float) -> None:
        log = get_logger("proxy")
        # Wait one full interval first: short-lived CLI processes that merely
        # touch the pool never pay for a probe.
        while not self._monitor_stop.wait(period):
            checked = 0
            for node in list(self.nodes):
                if self._monitor_stop.is_set():
                    return
                self.check(node)
                checked += 1
            log.info("proxy pool checked", extra={"detail": {"nodes": checked}})

    def stop_monitor(self, timeout: float = 2.0) -> None:
        self._monitor_stop.set()
        with self._lock:
            monitor, self._monitor = self._monitor, None
        if monitor is not None and monitor.is_alive() and monitor is not threading.current_thread():
            monitor.join(timeout=timeout)

    def masked(self) -> list[dict[str, Any]]:
        return [n.masked() for n in self.nodes]


def _to_node(entry: Any) -> ProxyNode:
    if isinstance(entry, str):
        return ProxyNode(raw=entry.strip())
    return ProxyNode(
        raw=str(entry.get("url", "")).strip(),
        username=str(entry.get("username", "")),
        password=str(entry.get("password", "")),
        headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
    )


def load_entries(settings: Any | None = None) -> list[Any]:
    settings = settings or get_settings()
    path = Path(settings.proxy_pool_config_path)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload = payload.get("proxies", [])
            return [p for p in payload if p]
        except Exception as exc:
            get_logger("proxy").warning("proxy config invalid", extra={"error": str(exc), "error_code": "QFSD-3001"})
    raw = (settings.proxy_pool_list or "").strip()
    if not raw:
        return []
    if raw.startswith(("[", "{")):
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                payload = payload.get("proxies", [])
            return payload
        except json.JSONDecodeError:
            pass
    return [p.strip() for p in raw.split(";") if p.strip()]


_POOL: ProxyPool | None = None
_LOCK = threading.Lock()


def get_pool() -> ProxyPool:
    global _POOL
    with _LOCK:
        if _POOL is None:
            _POOL = ProxyPool.from_settings()
        pool = _POOL
    if pool.nodes:
        pool.start_monitor()
    return pool


def reset_pool() -> None:
    global _POOL
    with _LOCK:
        old, _POOL = _POOL, None
    if old is not None:
        old.stop_monitor()
