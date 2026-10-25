"""HTTP plumbing: per-source rate limits, retries, proxies and health accounting."""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .config import get_settings
from .errors import TickHubError

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
]

# source -> (min interval seconds, max calls per window, window seconds)
SOURCE_LIMITS: dict[str, tuple[float, int, int]] = {
    "eastmoney": (1.0, 30, 60),
    "cninfo": (0.6, 40, 60),
    "tencent": (0.15, 180, 60),
    "sina": (0.2, 150, 60),
    "ths": (0.4, 60, 60),
    "exchange": (0.5, 60, 60),
    "macro": (0.5, 60, 60),
    "wallstreetcn": (0.3, 90, 60),
    "cls": (0.3, 90, 60),
    "cctv": (0.5, 30, 60),
    "iwencai": (0.5, 30, 60),
    "shfe": (0.5, 60, 60),
    "ine": (0.5, 60, 60),
    "czce": (0.5, 60, 60),
    "cffex": (0.5, 60, 60),
    "gfex": (0.5, 60, 60),
    "sge": (0.5, 60, 60),
    "sse": (0.2, 120, 60),
    "swsresearch": (1.0, 6, 60),
}


class RateLimiter:
    """Sliding-window limiter with a minimum gap between calls."""

    def __init__(self, min_interval: float = 0.2, max_calls: int = 60, window: int = 60):
        self.min_interval = min_interval
        self.max_calls = max_calls
        self.window = window
        self._last = 0.0
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                time.sleep(wait + random.uniform(0.01, 0.08))
                now = time.monotonic()
            while self._calls and now - self._calls[0] > self.window:
                self._calls.popleft()
            if len(self._calls) >= self.max_calls:
                sleep_for = self.window - (now - self._calls[0]) + 0.05
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
                while self._calls and now - self._calls[0] > self.window:
                    self._calls.popleft()
            self._calls.append(time.monotonic())
            self._last = time.monotonic()


_LIMITERS: dict[str, RateLimiter] = {}
_LIMIT_LOCK = threading.Lock()


def limiter_for(source: str) -> RateLimiter:
    with _LIMIT_LOCK:
        if source not in _LIMITERS:
            cfg = SOURCE_LIMITS.get(source, (0.3, 60, 60))
            _LIMITERS[source] = RateLimiter(*cfg)
        return _LIMITERS[source]


# push2 quotes are throttled upstream per source IP *and endpoint group*: one
# burst silences every caller of that path for minutes, while sibling paths keep
# answering. Pace each path separately instead of leaning on the 1s source gate.
_PUSH2_HOSTS = frozenset({"push2.eastmoney.com", "push2his.eastmoney.com", "push2ex.eastmoney.com"})
_PUSH2_EXEMPT_PATHS = frozenset({"/api/qt/kamt/get", "/api/qt/slist/get"})
_NP_SUFFIX_RE = re.compile(r"\.np(?=/|$)")

ENDPOINT_MIN_INTERVAL = 25.0
ENDPOINT_MAX_CALLS = 3
ENDPOINT_WINDOW = 60
ENDPOINT_BREAKER_SECONDS = 600.0


def endpoint_family(url: str) -> str | None:
    """Throttling group for ``url``, or ``None`` when it is not a push2 quote path."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in _PUSH2_HOSTS:
        return None
    path = _NP_SUFFIX_RE.sub("", parsed.path.rstrip("/"))
    if path in _PUSH2_EXEMPT_PATHS:
        return None
    return f"{host}{path}"


_ENDPOINT_LIMITERS: dict[str, RateLimiter] = {}
_ENDPOINT_LIMIT_LOCK = threading.Lock()


def endpoint_limiter(family: str) -> RateLimiter:
    with _ENDPOINT_LIMIT_LOCK:
        if family not in _ENDPOINT_LIMITERS:
            _ENDPOINT_LIMITERS[family] = RateLimiter(
                ENDPOINT_MIN_INTERVAL, ENDPOINT_MAX_CALLS, ENDPOINT_WINDOW
            )
        return _ENDPOINT_LIMITERS[family]


class EndpointBreaker:
    """Cools an endpoint family down for a fixed window after a connection reset.

    The upstream penalty latches for minutes, so retrying only burns attempts and
    deepens the block. Tripping on the first reset is deliberate.
    """

    def __init__(self, cooldown: float = ENDPOINT_BREAKER_SECONDS) -> None:
        self.cooldown = cooldown
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def remaining(self, family: str) -> float:
        with self._lock:
            until = self._until.get(family)
            if until is None:
                return 0.0
            left = until - time.monotonic()
            if left <= 0:
                self._until.pop(family, None)
                return 0.0
            return left

    def trip(self, family: str) -> float:
        with self._lock:
            self._until[family] = time.monotonic() + self.cooldown
            return self.cooldown

    def open_families(self) -> dict[str, int]:
        with self._lock:
            now = time.monotonic()
            return {
                family: int(until - now + 0.999)
                for family, until in self._until.items()
                if until > now
            }

    def reset(self) -> None:
        with self._lock:
            self._until.clear()


ENDPOINT_BREAKER = EndpointBreaker()

_RESET_MARKERS = (
    "connection reset",
    "connection aborted",
    "reset by peer",
    "remote end closed connection",
    "remotedisconnected",
    "broken pipe",
)


def _is_connection_reset(exc: BaseException) -> bool:
    if not isinstance(exc, (ConnectionResetError, requests.exceptions.ConnectionError)):
        return False
    text = f"{exc.__class__.__name__} {exc}".lower()
    return any(marker in text for marker in _RESET_MARKERS)


_RESET_CLASSES = frozenset(
    {"ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError", "RemoteDisconnected"}
)


def is_connection_reset(exc: BaseException) -> bool:
    """Reset test that also understands the errors ``fetch`` boxes into ``TickHubError``.

    ``fetch`` reports transport trouble through ``detail["error"]`` (the original
    class name) and its message, so the reset markers still apply; an HTTP status
    error never matches because neither its class nor its text mentions a reset.
    """
    if isinstance(exc, TickHubError):
        name = str((exc.detail or {}).get("error") or exc.__class__.__name__)
    else:
        name = exc.__class__.__name__
    if name in _RESET_CLASSES:
        return True
    text = f"{name} {exc}".lower()
    return any(marker in text for marker in _RESET_MARKERS)


def reset_endpoint_guard() -> None:
    """Clear limiters and breaker windows (tests / admin maintenance)."""
    ENDPOINT_BREAKER.reset()
    with _ENDPOINT_LIMIT_LOCK:
        _ENDPOINT_LIMITERS.clear()


class HealthRegistry:
    """Source health counters, mirrored to a file for the admin console.

    The MCP server and the admin console are separate processes. Cache-backed
    health only crosses that boundary when Redis is up, so the counters are
    also written to ``HEALTH_STATE_PATH``; the console reads that file.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] = {}
        self._path = Path(path) if path else None
        self._dirty = False
        self._flushed = 0.0

    def record(self, source: str, ok: bool, latency_ms: float, error: str | None = None) -> None:
        with self._lock:
            cur = self._state.setdefault(
                source,
                {"source": source, "ok": True, "failures": 0, "successes": 0, "last_error": None},
            )
            cur["ok"] = ok
            cur["last_latency_ms"] = round(latency_ms, 1)
            cur["last_checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            if ok:
                cur["successes"] += 1
                cur["failures"] = 0
            else:
                cur["failures"] += 1
                cur["last_error"] = error
            self._dirty = True
            self._flush_locked()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}

    def healthy(self, source: str) -> bool:
        with self._lock:
            return self._state.get(source, {}).get("ok", True)

    def flush(self) -> None:
        with self._lock:
            self._flush_locked(force=True)

    def _flush_locked(self, force: bool = False) -> None:
        if not self._dirty:
            return
        now = time.monotonic()
        if not force and now - self._flushed < 2.0:
            return
        path = self._path or Path(get_settings().health_state_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            payload = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "sources": self._state}
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            self._dirty = False
            self._flushed = now
        except OSError:
            pass


HEALTH = HealthRegistry()


def read_health_state(path: str | Path | None = None, *, max_age_s: float = 86400) -> dict[str, dict[str, Any]]:
    """Health counters written by another process, ignored once stale."""
    p = Path(path) if path else Path(get_settings().health_state_path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sources = raw.get("sources")
    if not isinstance(sources, dict):
        return {}
    updated = str(raw.get("updated_at") or "")
    if updated and max_age_s > 0:
        try:
            written = time.mktime(time.strptime(updated[:19], "%Y-%m-%dT%H:%M:%S"))
            if time.time() - written > max_age_s:
                return {}
        except ValueError:
            pass
    return {str(k): v for k, v in sources.items() if isinstance(v, dict)}


def _proxy_for(
    source: str, exclude: set[str] | frozenset[str] = frozenset()
) -> tuple[dict[str, str] | None, Any | None]:
    """Return ``(proxies, node)``; ``node`` is set only when the pool supplied it.

    ``exclude`` lists node ids that already failed earlier in this request.
    """
    settings = get_settings()
    try:
        from ..proxy.pool import get_pool

        pool = get_pool()
        node = pool.acquire(exclude)
        if node:
            return node.as_requests_proxy(), node
    except Exception:
        pass
    if settings.proxy_url:
        return {"http": settings.proxy_url, "https": settings.proxy_url}, None
    return None, None


# Only transport-level failures implicate the proxy. An HTTP status, however
# unhappy, proves the request travelled through the tunnel.
PROXY_FAULTS = (
    requests.exceptions.ProxyError,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.SSLError,
)


def _mark_node(node: Any, ok: bool, latency_ms: float | None = None) -> None:
    try:
        from ..proxy.pool import get_pool

        get_pool().mark(node, ok, latency_ms)
    except Exception:  # noqa: BLE001
        pass


def _session_for(source: str) -> requests.Session:
    # One session per thread/source keeps connection reuse while staying fork-safe.
    tid = threading.get_ident()
    key = (source, tid)
    with _SESSION_LOCK:
        sess = _SESSIONS.get(key)
        if sess is None:
            sess = requests.Session()
            # Proxies come from the configured pool; ambient HTTP(S)_PROXY values
            # are often shared by unrelated tooling and break upstream routing.
            sess.trust_env = False
            sess.headers.update(
                {
                    "User-Agent": random.choice(UA_POOL),
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                }
            )
            _SESSIONS[key] = sess
        return sess


_SESSIONS: dict[tuple[str, int], requests.Session] = {}
_SESSION_LOCK = threading.Lock()


def fetch(
    source: str,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    retries: int | None = None,
    allow_status: tuple[int, ...] = (),
    allow_redirects: bool = True,
    verify: bool | str = True,
) -> requests.Response:
    """Throttled fetch returning the raw response.

    ``allow_status`` marks statuses that are a normal answer (an exchange file
    that does not exist that day) rather than a failure, so callers can tell a
    non-trading day apart from a broken endpoint.

    push2 quote paths get an extra guard: a per-path rate gate plus a breaker
    that opens on the first connection reset. While open, calls raise
    ``QFSD-7001`` before any retry, so the tool wrapper can serve stale cache.
    """
    settings = get_settings()
    timeout = timeout or settings.http_timeout
    retries = settings.http_retries if retries is None else retries
    family = endpoint_family(url)
    if family:
        cooldown = ENDPOINT_BREAKER.remaining(family)
        if cooldown > 0:
            raise TickHubError(
                "QFSD-7001",
                f"{family} 连接被重置，熔断中（约 {int(cooldown)} 秒后恢复）",
                detail={"url": url, "endpoint": family, "retry_after_s": int(cooldown) + 1},
                retryable=True,
            )
        endpoint_limiter(family).acquire()
    limiter_for(source).acquire()
    last_err: Exception | None = None
    tried: set[str] = set()

    for attempt in range(retries + 1):
        started = time.monotonic()
        proxies, node = _proxy_for(source, tried)
        try:
            resp = _session_for(source).request(
                method.upper(),
                url,
                params=params,
                data=data,
                headers=headers,
                timeout=timeout,
                proxies=proxies,
                allow_redirects=allow_redirects,
                verify=verify,
            )
            latency = (time.monotonic() - started) * 1000
            if node is not None:
                _mark_node(node, True, latency)
            if resp.status_code in allow_status:
                HEALTH.record(source, True, latency)
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            HEALTH.record(source, True, latency)
            return resp
        except Exception as exc:  # noqa: BLE001
            latency = (time.monotonic() - started) * 1000
            last_err = exc
            HEALTH.record(source, False, latency, str(exc))
            if node is not None and isinstance(exc, PROXY_FAULTS):
                _mark_node(node, False)
                tried.add(node.id)
            if family and _is_connection_reset(exc):
                ENDPOINT_BREAKER.trip(family)
                raise TickHubError(
                    "QFSD-7001",
                    f"{family} 连接被重置，已熔断 {int(ENDPOINT_BREAKER_SECONDS)} 秒",
                    detail={
                        "url": url,
                        "endpoint": family,
                        "retry_after_s": int(ENDPOINT_BREAKER_SECONDS),
                    },
                    retryable=True,
                ) from exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 4) + random.uniform(0, 0.3))
                continue
    timeout_err = isinstance(last_err, requests.Timeout)
    status = getattr(getattr(last_err, "response", None), "status_code", None)
    raise TickHubError(
        "QFSD-1001" if timeout_err else "QFSD-9003",
        f"{source} request failed: {last_err}",
        detail={
            "url": url,
            "status": status,
            "error": last_err.__class__.__name__ if last_err else None,
        },
        retryable=True,
    )


def request(
    source: str,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    retries: int | None = None,
    encoding: str | None = None,
    expect_json: bool = True,
) -> Any:
    resp = fetch(
        source,
        method,
        url,
        params=params,
        data=data,
        headers=headers,
        timeout=timeout,
        retries=retries,
    )
    try:
        if encoding:
            resp.encoding = encoding
        if not expect_json:
            return resp.text
        text = resp.text.strip()
        if text.startswith("var ") and "=" in text:
            text = text.split("=", 1)[1].strip().rstrip(";")
        if text.startswith(("jQuery", "cb(")) or (text and text[0] not in "{["):
            start = min([i for i in (text.find("{"), text.find("[")) if i >= 0] or [-1])
            if start >= 0:
                text = text[start:]
                text = text.rstrip(");")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Some endpoints wrap JSON in a JS callback; salvage the object.
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
            raise TickHubError("QFSD-9003", f"invalid JSON from {url}", detail=text[:200])
    except TickHubError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TickHubError("QFSD-9003", f"invalid response from {url}: {exc}", detail=resp.text[:200]) from exc


def em_get(url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    """Eastmoney GET, always routed through the shared throttle."""
    return request("eastmoney", "GET", url, params=params, **kwargs)


def em_datacenter(
    report_name: str,
    *,
    columns: str = "ALL",
    filter_str: str = "",
    page_size: int = 50,
    page_number: int = 1,
    sort_columns: str = "",
    sort_types: str = "-1",
    source: str = "eastmoney",
) -> list[dict[str, Any]]:
    params = {
        "reportName": report_name,
        "columns": columns,
        "filter": filter_str,
        "pageNumber": str(page_number),
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    data = em_get("https://datacenter-web.eastmoney.com/api/data/v1/get", params=params)
    result = (data or {}).get("result") or {}
    return result.get("data") or []


def source_of(url: str) -> str:
    host = urlparse(url).hostname or ""
    if "eastmoney" in host:
        return "eastmoney"
    if "cninfo" in host:
        return "cninfo"
    if "gtimg" in host or "qq.com" in host:
        return "tencent"
    if "sina" in host:
        return "sina"
    if "10jqka" in host:
        return "ths"
    if "wallstreetcn" in host:
        return "wallstreetcn"
    if "cls.cn" in host:
        return "cls"
    if "cctv" in host:
        return "cctv"
    return "exchange"
