"""Optional model gateway.

TickHub-CN never routes financial numbers through a model. This gateway exists
only for long-text cleaning (announcements / research reports) and is dormant
unless ``MODEL_GATEWAY_ENABLE=true`` is set, so the default install pays zero
cost and makes zero model calls.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import get_settings
from ..logging.setup import get_logger

try:
    import toml
except Exception:  # pragma: no cover
    toml = None


@dataclass
class GatewayConfig:
    enable: bool = False
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""
    routing: dict[str, str] = field(default_factory=dict)
    prompt: str = ""
    config_path: str = "./model_gateway.toml"
    error: str | None = None

    def model_for(self, purpose: str) -> str:
        return self.routing.get(purpose) or self.default_model

    def masked(self) -> dict[str, Any]:
        return {
            "enable": self.enable,
            "base_url": self.base_url,
            "api_key": "***" if self.api_key else "",
            "default_model": self.default_model,
            "routing": dict(self.routing),
            "prompt": self.prompt,
            "config_path": self.config_path,
            "error": self.error,
        }


def _load_file(path: Path) -> tuple[dict[str, Any], str | None]:
    """Return ``(table, error)``; a malformed file must never break the server."""
    if not path.exists() or toml is None:
        return {}, None
    try:
        return toml.loads(path.read_text(encoding="utf-8")).get("model_gateway", {}) or {}, None
    except Exception as exc:
        get_logger("gateway").warning(
            "model gateway config invalid", extra={"error": str(exc), "error_code": "QFSD-3001"}
        )
        return {}, str(exc)[:200]


def load_gateway_config() -> GatewayConfig:
    settings = get_settings()
    path = Path(settings.model_gateway_config_path or "./model_gateway.toml")
    file_conf, error = _load_file(path)
    cleaning = file_conf.get("cleaning") or {}
    enable = settings.model_gateway_enable or bool(file_conf.get("enable"))
    # MODEL_GATEWAY_* is the current name; CRAWL4AI_* kept as a legacy alias.
    base_url = (
        settings.model_gateway_base_url
        or settings.crawl4ai_base_url
        or str(file_conf.get("base_url") or "")
    )
    api_key = (
        settings.model_gateway_api_key
        or settings.crawl4ai_api_key
        or str(file_conf.get("api_key") or "")
    )
    default_model = settings.model_gateway_default_model or str(file_conf.get("default_model") or "")
    routing = {
        str(k): str(v) for k, v in (file_conf.get("routing") or {}).items() if v
    }
    # An explicitly set CRAWL4AI_MODEL overrides routing; the field has an
    # empty default so it cannot shadow a model chosen in the config file.
    env_model = os.environ.get("CRAWL4AI_MODEL", "").strip()
    if env_model:
        routing["text_clean"] = env_model
    prompt = str(cleaning.get("prompt") or file_conf.get("prompt") or "")
    return GatewayConfig(
        enable=enable,
        base_url=base_url,
        api_key=api_key,
        default_model=default_model,
        routing=routing,
        prompt=prompt,
        config_path=str(path),
        error=error,
    )


# The admin console runs in its own process, so a cached config would keep the
# MCP server on the old switch until restart. Keying the cache on the file's
# mtime/size lets a UI save take effect everywhere within one call.
_CACHE_LOCK = threading.Lock()
_CACHE: tuple[tuple[int, int] | None, GatewayConfig] | None = None


def get_gateway_config() -> GatewayConfig:
    global _CACHE
    path = Path(get_settings().model_gateway_config_path or "./model_gateway.toml")
    try:
        stat = path.stat()
        key: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    with _CACHE_LOCK:
        if _CACHE is not None and _CACHE[0] == key:
            return _CACHE[1]
    conf = load_gateway_config()
    with _CACHE_LOCK:
        _CACHE = (key, conf)
    return conf


def reload_gateway_config() -> GatewayConfig:
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None
    return get_gateway_config()


def enabled() -> bool:
    conf = get_gateway_config()
    return bool(conf.enable and conf.base_url)


def chat(
    messages: list[dict[str, str]],
    *,
    purpose: str = "text_clean",
    model: str | None = None,
    timeout: float = 60.0,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Call an OpenAI-compatible ``/chat/completions`` endpoint."""
    conf = get_gateway_config()
    if not conf.enable:
        raise RuntimeError("model gateway disabled")
    if not conf.base_url:
        raise RuntimeError("model gateway base_url is empty")
    chosen = model or conf.model_for(purpose)
    if not chosen:
        raise RuntimeError(f"no model configured for purpose {purpose}")
    import requests

    url = conf.base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if conf.api_key:
        headers["Authorization"] = f"Bearer {conf.api_key}"
    body = {
        "model": chosen,
        "messages": messages,
        "temperature": temperature,
    }
    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    content = (
        (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if isinstance(payload, dict)
        else ""
    )
    return {"model": chosen, "content": content, "usage": payload.get("usage") if isinstance(payload, dict) else None}


def status() -> dict[str, Any]:
    conf = get_gateway_config()
    return {"enabled": enabled(), **conf.masked()}


__all__ = [
    "GatewayConfig",
    "chat",
    "enabled",
    "get_gateway_config",
    "load_gateway_config",
    "reload_gateway_config",
    "status",
]
