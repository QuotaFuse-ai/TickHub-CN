"""Tiered cache: Redis when available, in-process TTL store otherwise."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from ..core.config import get_settings
from ..logging.setup import get_logger

TTL = {
    "static": 7 * 24 * 3600,
    "semi_static": 24 * 3600,
    "realtime": 60,
    "health": 120,
}


def params_hash(params: dict[str, Any] | None) -> str:
    if not params:
        return "default"
    blob = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


# Expired entries are dropped lazily. Purging every write would be O(n) per
# call, so the sweep runs once per this many writes and on every inspection.
_PRUNE_EVERY = 512


class _MemoryStore:
    def __init__(self) -> None:
        self.data: dict[str, tuple[float | None, Any]] = {}
        self.lock = threading.Lock()
        self._writes = 0

    def _prune(self, now: float) -> None:
        dead = [k for k, (exp, _) in self.data.items() if exp is not None and exp <= now]
        for key in dead:
            self.data.pop(key, None)

    def get(self, key: str) -> Any:
        with self.lock:
            item = self.data.get(key)
            if not item:
                return None
            expires, value = item
            if expires is not None and time.time() > expires:
                self.data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: int | None) -> None:
        with self.lock:
            now = time.time()
            self.data[key] = (now + ttl if ttl else None, value)
            self._writes += 1
            if self._writes >= _PRUNE_EVERY:
                self._writes = 0
                self._prune(now)

    def delete(self, key: str) -> None:
        with self.lock:
            self.data.pop(key, None)

    def keys(self, prefix: str) -> list[str]:
        with self.lock:
            now = time.time()
            self._prune(now)
            return [k for k, (exp, _) in self.data.items() if k.startswith(prefix) and (exp is None or exp > now)]


class Cache:
    def __init__(self, url: str | None = None, env: str | None = None) -> None:
        settings = get_settings()
        self.env = env or settings.cache_env
        self.url = url if url is not None else settings.redis_url
        self.memory = _MemoryStore()
        self.redis = None
        self.redis_ok = False
        self.hits = 0
        self.misses = 0
        self._locks: dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()
        self._connect()

    def _connect(self) -> None:
        if not self.url:
            get_logger("cache").warning("cache degraded to memory", extra={"event": "redis_unavailable", "error_code": "QFSD-2001"})
            return
        try:
            import redis

            client = redis.Redis.from_url(self.url, socket_connect_timeout=1.2, socket_timeout=1.5, decode_responses=True)
            client.ping()
            self.redis = client
            self.redis_ok = True
        except Exception as exc:
            self.redis = None
            self.redis_ok = False
            get_logger("cache").warning(
                "cache degraded to memory", extra={"event": "redis_unavailable", "error_code": "QFSD-2001", "error": str(exc)[:120]}
            )

    def key(self, domain: str, code: str = "-", granularity: str = "-", params: dict[str, Any] | None = None) -> str:
        return f"qf:{self.env}:cache:{domain}:{code}:{granularity}:{params_hash(params)}"

    def get(self, key: str) -> tuple[Any, bool]:
        if self.redis_ok and self.redis is not None:
            try:
                raw = self.redis.get(key)
                if raw is None:
                    self.misses += 1
                    return None, False
                self.hits += 1
                return json.loads(raw), True
            except Exception:
                self.redis_ok = False
        value = self.memory.get(key)
        if value is None:
            self.misses += 1
            return None, False
        self.hits += 1
        return value, True

    def set(self, key: str, value: Any, ttl: int = TTL["semi_static"]) -> None:
        blob = json.dumps(value, ensure_ascii=False, default=str)
        if self.redis_ok and self.redis is not None:
            try:
                self.redis.setex(key, ttl, blob)
                return
            except Exception:
                self.redis_ok = False
        self.memory.set(key, value, ttl)

    def delete(self, key: str) -> None:
        if self.redis_ok and self.redis is not None:
            try:
                self.redis.delete(key)
            except Exception:
                self.redis_ok = False
        self.memory.delete(key)

    def remember(self, key: str, ttl: int, producer: Callable[[], Any], *, refresh: bool = False) -> tuple[Any, bool]:
        if not refresh:
            value, hit = self.get(key)
            if hit:
                return value, True
        value = producer()
        self.set(key, value, ttl)
        return value, False

    def flush(self, prefix: str | None = None) -> int:
        pattern = f"qf:{self.env}:cache:{prefix or ''}*"
        removed = 0
        if self.redis_ok and self.redis is not None:
            try:
                keys = list(self.redis.scan_iter(match=pattern, count=500))
                if keys:
                    removed += int(self.redis.delete(*keys))
            except Exception:
                self.redis_ok = False
        for key in self.memory.keys(f"qf:{self.env}:cache:{prefix or ''}"):
            self.memory.delete(key)
            removed += 1
        return removed

    def stats(self) -> dict[str, Any]:
        total = max(self.hits + self.misses, 1)
        info: dict[str, Any] = {
            "backend": "redis" if self.redis_ok else "memory",
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4),
            "env": self.env,
        }
        if self.redis_ok and self.redis is not None:
            try:
                info["keys"] = self.redis.dbsize()
            except Exception:
                pass
        else:
            info["keys"] = len(self.memory.keys(f"qf:{self.env}:"))
        return info

    @contextmanager
    def lock(self, name: str, timeout: int = 30, ttl: int = 60) -> Iterator[bool]:
        """Best-effort distributed anti-duplicate crawl lock."""
        lock_key = f"qf:{self.env}:lock:crawl:{name}"
        if self.redis_ok and self.redis is not None:
            token = str(time.time())
            acquired = False
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    acquired = bool(self.redis.set(lock_key, token, nx=True, ex=ttl))
                except Exception:
                    self.redis_ok = False
                    break
                if acquired:
                    break
                time.sleep(0.2)
            try:
                yield acquired
            finally:
                if acquired:
                    try:
                        self.redis.delete(lock_key)
                    except Exception:
                        pass
            return
        with self._lock_guard:
            local = self._locks.setdefault(name, threading.Lock())
        acquired = local.acquire(timeout=timeout)
        try:
            yield acquired
        finally:
            if acquired:
                local.release()

    def enqueue(self, task: str, payload: dict[str, Any]) -> None:
        item = json.dumps(payload, ensure_ascii=False)
        if self.redis_ok and self.redis is not None:
            try:
                self.redis.rpush(f"qf:{self.env}:queue:offline:{task}", item)
                return
            except Exception:
                self.redis_ok = False
        key = f"qf:{self.env}:queue:offline:{task}:mem"
        queue = self.memory.get(key) or []
        queue.append(payload)
        self.memory.set(key, queue, None)

    def dedupe(self, source: str, item_id: str, ttl: int = 7 * 24 * 3600) -> bool:
        """Return True when this item has not been seen before."""
        key = f"qf:{self.env}:dedup:{source}:{item_id}"
        if self.redis_ok and self.redis is not None:
            try:
                return bool(self.redis.set(key, "1", nx=True, ex=ttl))
            except Exception:
                self.redis_ok = False
        if self.memory.get(key):
            return False
        self.memory.set(key, "1", ttl)
        return True

    def queue_length(self, task: str) -> int:
        if self.redis_ok and self.redis is not None:
            try:
                return int(self.redis.llen(f"qf:{self.env}:queue:offline:{task}"))
            except Exception:
                self.redis_ok = False
        return len(self.memory.get(f"qf:{self.env}:queue:offline:{task}:mem") or [])

    def record_health(self, source: str, payload: dict[str, Any]) -> None:
        key = f"qf:{self.env}:health:{source}"
        self.set(key, payload, TTL["health"])

    def read_health(self) -> dict[str, Any]:
        if self.redis_ok and self.redis is not None:
            try:
                out = {}
                for key in self.redis.scan_iter(match=f"qf:{self.env}:health:*", count=100):
                    raw = self.redis.get(key)
                    if raw:
                        out[str(key).rsplit(":", 1)[-1]] = json.loads(raw)
                return out
            except Exception:
                self.redis_ok = False
        prefix = f"qf:{self.env}:health:"
        return {k[len(prefix):]: self.memory.get(k) for k in self.memory.keys(prefix)}


_CACHE: Cache | None = None
_CACHE_LOCK = threading.Lock()


def get_cache() -> Cache:
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None:
            _CACHE = Cache()
        return _CACHE


def reset_cache() -> None:
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None
