"""Optional model gateway.

TickHub-CN never routes financial numbers through a model. This gateway exists
only for long-text cleaning (announcements / research reports) and is dormant
unless ``MODEL_GATEWAY_ENABLE=true`` is set, so the default install pays zero
cost and makes zero model calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
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
            "config_path": self.config_path,
            "error": self.error,
        }


def _load_file(path: Path) -> dict[str, Any]:
    if not path.exists() or toml is None:
        return {}
    try:
        return toml.loads(path.read_text(encoding="utf-8")).get("model_gateway", {}) or {}
    except Exception as exc:  # malformed config must never break the server
        get_logger("gateway").warning(
            "model gateway config invalid", extra={"error": str(exc), "error_code": "QFSD-3001"}
        )
        return {}


def load_gateway_config() -> GatewayConfig:
    settings = get_settings()
    path = Path(settings.model_gateway_config_path or "./model_gateway.toml")
    file_conf = _load_file(path)
    enable = settings.model_gateway_enable or bool(file_conf.get("enable"))
    base_url = settings.model_gateway_base_url or str(file_conf.get("base_url") or "")
    api_key = settings.model_gateway_api_key or str(file_conf.get("api_key") or "")
    default_model = settings.model_gateway_default_model or str(file_conf.get("default_model") or "")
    routing = {
        str(k): str(v) for k, v in (file_conf.get("routing") or {}).items() if v
    }
    # Env-var routing overrides for the two documented purposes.
    if settings.crawl4ai_model:
        routing.setdefault("text_clean", settings.crawl4ai_model)
    return GatewayConfig(
        enable=enable,
        base_url=base_url,
        api_key=api_key,
        default_model=default_model,
        routing=routing,
        config_path=str(path),
    )


@lru_cache(maxsize=1)
def get_gateway_config() -> GatewayConfig:
    return load_gateway_config()


def reload_gateway_config() -> GatewayConfig:
    get_gateway_config.cache_clear()
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
