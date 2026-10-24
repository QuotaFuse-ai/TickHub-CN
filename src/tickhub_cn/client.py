"""In-process Python client.

Importing ``tickhub_cn`` must stay side-effect free, so the data-source modules
(which register the tools) are only imported on first use.
"""

from __future__ import annotations

from typing import Any


class StockDataClient:
    """Call any TickHub-CN tool without running a server.

    >>> client = StockDataClient()
    >>> client.tencent_quote("600000")["data"][0]["price"]
    >>> client.call("tencent_kline", {"code": "600000", "period": "daily"})
    """

    def __init__(self) -> None:
        self._loaded = False

    def _load(self) -> None:
        if not self._loaded:
            from . import datasources  # noqa: F401  (registers every tool)

            self._loaded = True

    def tools(self) -> list[dict[str, Any]]:
        self._load()
        from .datasources.base import tool_catalog

        return tool_catalog()

    def layers(self) -> dict[str, list[str]]:
        self._load()
        from .datasources.base import LAYERS

        return {name: list(tools) for name, tools in LAYERS.items()}

    def call(self, name: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self._load()
        from .datasources.base import call_tool

        merged = dict(params or {})
        merged.update(kwargs)
        return call_tool(name, merged)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        self._load()
        from .datasources.base import REGISTRY

        if name not in REGISTRY:
            raise AttributeError(f"unknown tool: {name}")
        return REGISTRY[name].func

    def __dir__(self) -> list[str]:
        self._load()
        from .datasources.base import tool_names

        return sorted(set(super().__dir__()) | set(tool_names()))


__all__ = ["StockDataClient"]
