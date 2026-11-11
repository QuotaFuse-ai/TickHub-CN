"""Config-driven upstream exit: ``pool`` (self-hosted free pool), ``commercial``
or ``direct``.

The provider is chosen by ``PROXY_PROVIDER``; switching providers is a config
change, never a code change. A commercial exit keeps its URL/credentials (or
IP whitelist) in config and is only used when ``provider=commercial`` *and*
``PROXY_COMMERCIAL_URL`` is set.
"""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

from ..core.config import Settings, get_settings
from ..logging.setup import get_logger

VALID_PROVIDERS = ("pool", "commercial", "direct")

_log = get_logger("proxy")
_log_lock = threading.Lock()
_last_selection = ""


def resolve_provider(settings: Settings | None = None) -> str:
    """Effective provider; blank falls back to the legacy enable flag."""
    settings = settings or get_settings()
    raw = str(getattr(settings, "proxy_provider", "") or "").strip().lower()
    if raw:
        return raw
    return "pool" if getattr(settings, "proxy_pool_enable", False) else "direct"


def safe_url(raw: str) -> str:
    """Host:port only - never echo credentials from a proxy URL."""
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.hostname:
        return raw.split("@")[-1] if "@" in raw else raw
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{parsed.hostname}:{port}" if port else str(parsed.hostname)
    return urlunparse(parsed._replace(netloc=netloc))


def _commercial_proxies(url: str, username: str, password: str) -> dict[str, str]:
    if username:
        parsed = urlparse(url)
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
        url = urlunparse(parsed._replace(netloc=auth + parsed.netloc))
    return {"http": url, "https": url}


def _pool_exit(exclude: frozenset[str] | set[str]) -> tuple[dict[str, str] | None, Any | None]:
    settings = get_settings()
    try:
        from .pool import get_pool

        node = get_pool().acquire(exclude)
        if node:
            return node.as_requests_proxy(), node
    except Exception:  # noqa: BLE001 - a broken pool must not break the request path
        pass
    pool_url = str(getattr(settings, "proxy_pool_url", "") or "").strip()
    if pool_url:
        return {"http": pool_url, "https": pool_url}, None
    if settings.proxy_url:
        return {"http": settings.proxy_url, "https": settings.proxy_url}, None
    return None, None


def _log_selection(key: str, detail: dict[str, Any]) -> None:
    """Log the active exit once per distinct configuration, not per request."""
    global _last_selection
    with _log_lock:
        if key == _last_selection:
            return
        _last_selection = key
    _log.info("proxy provider selected", extra={"detail": detail})


def select_exit(
    exclude: frozenset[str] | set[str] = frozenset(),
) -> tuple[dict[str, str] | None, Any | None]:
    """Return ``(proxies, node)`` for the configured provider.

    ``node`` is only set when the pool supplied it. ``direct`` returns no
    proxies on purpose; callers pass the result straight to ``requests``.
    """
    settings = get_settings()
    provider = resolve_provider(settings)

    if provider == "direct":
        _log_selection("direct", {"provider": "direct"})
        return None, None

    if provider == "commercial":
        url = str(getattr(settings, "proxy_commercial_url", "") or "").strip()
        username = str(getattr(settings, "proxy_commercial_username", "") or "")
        password = str(getattr(settings, "proxy_commercial_password", "") or "")
        whitelist = [
            item.strip()
            for item in str(getattr(settings, "proxy_commercial_whitelist", "") or "").split(",")
            if item.strip()
        ]
        if url:
            _log_selection(
                f"commercial|{safe_url(url)}|{username}|{len(whitelist)}",
                {
                    "provider": "commercial",
                    "url": safe_url(url),
                    "auth": "basic" if username else ("whitelist" if whitelist else "none"),
                    "whitelist_entries": len(whitelist),
                },
            )
            return _commercial_proxies(url, username, password), None
        _log_selection(
            "commercial|unconfigured",
            {
                "provider": "commercial",
                "commercial_url": "",
                "fallback": "pool",
                "hint": "set PROXY_COMMERCIAL_URL to enable the commercial exit",
            },
        )
        # An unconfigured commercial exit must not take the service down; the
        # pool is the documented fallback and the fall-through is logged above.

    proxies, node = _pool_exit(exclude)
    pool_url = str(getattr(settings, "proxy_pool_url", "") or "").strip()
    pool = _pool_ref()
    _log_selection(
        f"pool|{safe_url(pool_url)}",
        {
            "provider": "pool" if provider != "commercial" else "pool (commercial unconfigured)",
            "pool_url": safe_url(pool_url),
            "nodes": len(getattr(pool, "nodes", []) or []),
        },
    )
    return proxies, node


def _pool_ref() -> Any:
    try:
        from .pool import get_pool

        return get_pool()
    except Exception:  # noqa: BLE001
        return None
