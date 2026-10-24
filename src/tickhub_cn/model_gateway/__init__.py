"""Optional model gateway (model routing only; never numeric extraction)."""

from .gateway import (
    GatewayConfig,
    chat,
    enabled,
    get_gateway_config,
    load_gateway_config,
    reload_gateway_config,
    status,
)

__all__ = [
    "GatewayConfig",
    "chat",
    "enabled",
    "get_gateway_config",
    "load_gateway_config",
    "reload_gateway_config",
    "status",
]
