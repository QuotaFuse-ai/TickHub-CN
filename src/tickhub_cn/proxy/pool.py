"""Authenticated proxy pool with health checks and direct-connect fallback."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlunparse

from ..core.config import get_settings
from ..logging.setup import get_logger

MASK = "***"


def _mask(value: str) -> str:
    if not value:
        return ""
    return value[:2] + MASK if len(value) > 4 else MASK


def _node_key(raw: str, username: str, headers: dict[str, str] | None = None) -> str:
    """Opaque, credential-free handle used by the console to address a node."""
    basis = "\x1f".join([raw, username, json.dumps(headers or {}, sort_keys=True, ensure_ascii=False)])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


def _safe_url(raw: str) -> str:
    """Drop any userinfo; keep host and port so operators can identify a node."""
    parsed = urlparse(raw)
    if not parsed.hostname:
        return raw.split("@")[-1] if "@" in raw else raw
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{parsed.hostname}:{port}" if port else str(parsed.hostname)
    return urlunparse(parsed._replace(netloc=netloc))


@dataclass
class ProxyNode:
    raw: str
    username: str = ""
    password: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    healthy: bool = True
    failures: int = 0
    latency_ms: float | None = None
    last_checked: float | None = None

    @property
    def id(self) -> str:
        host = urlparse(self.raw).netloc or self.raw
        return f"{host}|{_mask(self.username)}"

    @property
    def key(self) -> str:
        return _node_key(self.raw, self.username, self.headers)

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
        return {
            "id": self.id,
            "key": self.key,
            "url": _safe_url(self.raw),
            "username": _mask(self.username),
            "has_password": bool(self.password),
            "headers": {k: MASK for k in self.headers},
            "enabled": self.enabled,
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

    def acquire(self, exclude: set[str] | frozenset[str] = frozenset()) -> ProxyNode | None:
        """Round-robin over healthy nodes, skipping ids in ``exclude``.

        ``exclude`` carries the exits that already failed within the current
        request, so a retry does not land on the same node again. If every
        healthy node is excluded, an excluded one is reused rather than
        returning None: going direct would silently bypass the pool.
        """
        if not self.nodes:
            return None
        with self._lock:
            n = len(self.nodes)
            fallback: ProxyNode | None = None
            for _ in range(n):
                node = self.nodes[self._idx % n]
                self._idx += 1
                if not node.enabled or not node.healthy:
                    continue
                if node.id in exclude:
                    if fallback is None:
                        fallback = node
                    continue
                return node
            if fallback is not None:
                return fallback
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
                if not node.enabled:
                    continue
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
        username=str(entry.get("username", "") or ""),
        password=str(entry.get("password", "") or ""),
        headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
        enabled=bool(entry.get("enabled", not entry.get("disabled", False))),
    )


def _entry_dict(entry: Any) -> dict[str, Any]:
    """Tolerant normalisation used for reads; never raises on a legacy entry."""
    if isinstance(entry, str):
        return {"url": entry.strip(), "username": "", "password": "", "headers": {}, "enabled": True}
    if not isinstance(entry, dict):
        return {"url": "", "username": "", "password": "", "headers": {}, "enabled": True}
    headers = entry.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}
    return {
        "url": str(entry.get("url", "") or "").strip(),
        "username": str(entry.get("username", "") or ""),
        "password": str(entry.get("password", "") or ""),
        "headers": {str(k): str(v) for k, v in headers.items()},
        "enabled": bool(entry.get("enabled", not entry.get("disabled", False))),
    }


def _entry_key(entry: Any) -> str:
    d = _entry_dict(entry)
    return _node_key(d["url"], d["username"], d["headers"])


def masked_entry(entry: Any) -> dict[str, Any]:
    d = _entry_dict(entry)
    return {
        "key": _node_key(d["url"], d["username"], d["headers"]),
        "url": _safe_url(d["url"]),
        "username": _mask(d["username"]),
        "has_password": bool(d["password"]),
        "headers": {k: MASK for k in d["headers"]},
        "enabled": d["enabled"],
    }


def normalize_entry(entry: Any) -> dict[str, Any]:
    """Validate a console-supplied node and split inline credentials out."""
    d = _entry_dict(entry)
    if d["url"] and "://" not in d["url"]:
        d["url"] = "http://" + d["url"]
    parsed = urlparse(d["url"])
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("proxy url must be http(s)://host[:port]")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("proxy url has an invalid port") from exc
    if parsed.username:
        if not d["username"]:
            d["username"] = unquote(parsed.username)
            d["password"] = unquote(parsed.password or "")
        netloc = f"{parsed.hostname}:{port}" if port else str(parsed.hostname)
        d["url"] = urlunparse(parsed._replace(netloc=netloc))
    if not d["url"]:
        raise ValueError("proxy url is required")
    return d


def write_entries(entries: list[Any], settings: Any | None = None) -> Path:
    """Atomically persist the node list in the format ``load_entries`` reads."""
    settings = settings or get_settings()
    path = Path(settings.proxy_pool_config_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps({"proxies": list(entries)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def add_proxy_node(entry: Any, settings: Any | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    node = normalize_entry(entry)
    entries = list(load_entries(settings))
    key = _node_key(node["url"], node["username"], node["headers"])
    if any(_entry_key(e) == key for e in entries):
        raise ValueError("proxy node already exists")
    entries.append(node)
    write_entries(entries, settings)
    return masked_entry(node)


def remove_proxy_node(key: str, settings: Any | None = None) -> dict[str, Any] | None:
    settings = settings or get_settings()
    kept: list[Any] = []
    removed: dict[str, Any] | None = None
    for entry in load_entries(settings):
        if removed is None and _entry_key(entry) == key:
            removed = masked_entry(entry)
            continue
        kept.append(entry)
    if removed is None:
        return None
    write_entries(kept, settings)
    return removed


def set_proxy_node_enabled(key: str, enabled: bool, settings: Any | None = None) -> dict[str, Any] | None:
    settings = settings or get_settings()
    entries: list[Any] = []
    updated: dict[str, Any] | None = None
    for entry in load_entries(settings):
        if updated is None and _entry_key(entry) == key:
            d = _entry_dict(entry)
            d["enabled"] = bool(enabled)
            entries.append(d)
            updated = masked_entry(d)
        else:
            entries.append(entry)
    if updated is None:
        return None
    write_entries(entries, settings)
    return updated


def describe_entries(settings: Any | None = None) -> list[dict[str, Any]]:
    """Merge persisted nodes with live health, including nodes the pool skipped.

    ``PROXY_POOL_ENABLE=false`` builds an empty runtime pool, but the file may
    still hold nodes; the console must show them (``loaded: false``) or they
    would look deleted.
    """
    settings = settings or get_settings()
    runtime = {node.key: node for node in get_pool().nodes}
    out: list[dict[str, Any]] = []
    for entry in load_entries(settings):
        row = masked_entry(entry)
        node = runtime.get(row["key"])
        if node is None:
            row.update({"healthy": None, "failures": 0, "latency_ms": None, "loaded": False})
        else:
            row.update(
                {
                    "enabled": node.enabled,
                    "healthy": node.healthy,
                    "failures": node.failures,
                    "latency_ms": node.latency_ms,
                    "loaded": True,
                }
            )
        out.append(row)
    return out


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
    raw = str(getattr(settings, "proxy_pool_list", "") or "").strip()
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
