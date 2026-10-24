"""Tool registry, caching/provenance wrapper and batch helpers.

Every public data function in ``tickhub_cn.datasources`` is registered here.
The wrapper adds the cross-cutting behaviour required by the contract:

* tiered cache (``refresh`` bypass)
* field projection from ``field_map.toml`` (``fields`` per call wins)
* point-in-time filtering (``data_as_of``)
* provenance metadata on every response
"""

from __future__ import annotations

import copy
import functools
import inspect
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..cache.redis_cache import TTL, get_cache
from ..core.errors import TickHubError, as_error
from ..core.provenance import as_of_filter, trace_id
from ..fieldmap.loader import get_field_map


@dataclass
class ToolSpec:
    name: str
    func: Callable[..., Any]
    description: str
    layer: str
    ttl: int | None
    params: tuple[str, ...] = field(default_factory=tuple)


REGISTRY: dict[str, ToolSpec] = {}
ALIASES: dict[str, str] = {}
LAYERS: dict[str, list[str]] = {}


def _signature_with_controls(sig: inspect.Signature) -> inspect.Signature:
    params = [p for p in sig.parameters.values() if p.name not in ("data_as_of", "fields")]
    params += [
        inspect.Parameter("data_as_of", inspect.Parameter.KEYWORD_ONLY, default="", annotation=str),
        inspect.Parameter("fields", inspect.Parameter.KEYWORD_ONLY, default="", annotation=str),
    ]
    return sig.replace(parameters=params)


def _apply_projection(tool_name: str, payload: Any, fields: str | list[str] | None) -> Any:
    if not isinstance(payload, dict) or "data" not in payload:
        return payload
    try:
        payload = copy.deepcopy(payload)
        payload["data"] = get_field_map().apply(tool_name, payload["data"], fields=fields)
    except Exception:  # projection must never break data delivery
        pass
    return payload


def _apply_as_of(payload: Any, data_as_of: str | None) -> Any:
    if not data_as_of or not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    if isinstance(data, list):
        payload["data"] = as_of_filter(data, data_as_of)
    elif isinstance(data, dict):
        for key, value in list(data.items()):
            if isinstance(value, list) and value and isinstance(value[0], dict):
                data[key] = as_of_filter(value, data_as_of)
    meta = payload.setdefault("meta", {})
    meta["data_as_of"] = data_as_of
    return payload


def tool(
    name: str | None = None,
    *,
    layer: str,
    ttl: str | int | None = "semi_static",
    cache_domain: str | None = None,
):
    """Register a data function as an MCP tool with cache + projection."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or fn.__name__
        sig = inspect.signature(fn)
        domain = cache_domain or tool_name.split("_")[0]
        ttl_seconds: int | None
        if ttl is None:
            ttl_seconds = None
        elif isinstance(ttl, str):
            ttl_seconds = TTL[ttl]
        else:
            ttl_seconds = int(ttl)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            refresh = bool(kwargs.pop("_refresh", False))
            no_cache = bool(kwargs.pop("_no_cache", False))
            fields = kwargs.pop("fields", "") or None
            data_as_of = kwargs.pop("data_as_of", "") or None
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            call_kwargs = dict(bound.arguments)

            def producer() -> Any:
                return fn(**call_kwargs)

            if ttl_seconds and not no_cache:
                code = str(
                    call_kwargs.get("code")
                    or call_kwargs.get("symbol")
                    or call_kwargs.get("index_code")
                    or call_kwargs.get("date")
                    or "-"
                )
                cache = get_cache()
                key = cache.key(domain, code, tool_name, call_kwargs)
                payload, hit = cache.remember(key, ttl_seconds, producer, refresh=refresh)
                if isinstance(payload, dict):
                    payload = copy.deepcopy(payload)
                    payload.setdefault("meta", {})["cache_hit"] = hit
            else:
                payload = producer()

            payload = _apply_as_of(payload, data_as_of)
            payload = _apply_projection(tool_name, payload, fields)
            return payload

        wrapper.__signature__ = _signature_with_controls(sig)  # type: ignore[attr-defined]
        REGISTRY[tool_name] = ToolSpec(
            name=tool_name,
            func=wrapper,
            description=(fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else tool_name,
            layer=layer,
            ttl=ttl_seconds,
            params=tuple(p.name for p in sig.parameters.values()),
        )
        LAYERS.setdefault(layer, []).append(tool_name)
        return wrapper

    return deco


def register_alias(alias: str, target: str) -> None:
    if target in REGISTRY:
        ALIASES[alias] = target


def resolve(name: str) -> ToolSpec:
    target = ALIASES.get(name, name)
    if target not in REGISTRY:
        raise TickHubError("QFSD-9002", f"unknown tool: {name}", detail={"available": len(REGISTRY)})
    return REGISTRY[target]


def tool_names() -> list[str]:
    return sorted(REGISTRY)


def tool_catalog() -> list[dict[str, Any]]:
    out = []
    for name in tool_names():
        spec = REGISTRY[name]
        out.append(
            {
                "name": name,
                "layer": spec.layer,
                "description": spec.description,
                "ttl_seconds": spec.ttl,
                "params": list(spec.params),
            }
        )
    return out


def call_tool(name: str, params: dict[str, Any] | None = None) -> Any:
    spec = resolve(name)
    return spec.func(**(params or {}))


def run_batch(
    codes: Iterable[str],
    fn: Callable[[str], Any],
    *,
    max_codes: int = 50,
    concurrency: int = 4,
    label: str = "code",
) -> dict[str, Any]:
    """Run a per-code function with bounded concurrency and partial results."""
    items = [str(c).strip() for c in codes if str(c).strip()]
    if len(items) > max_codes:
        raise TickHubError(
            "QFSD-9001",
            f"batch size {len(items)} exceeds limit {max_codes}",
            detail={"max": max_codes},
        )
    results: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    workers = max(1, min(concurrency, len(items) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, code): code for code in items}
        for future in as_completed(futures):
            code = futures[future]
            try:
                results[code] = future.result()
            except Exception as exc:  # noqa: BLE001
                failures.append({"code": code, "error": as_error(exc).to_dict()})
    payload: dict[str, Any] = {
        "ok": not failures,
        "data": {
            "total": len(items),
            "succeeded": len(results),
            "failed": len(failures),
            label + "s": results,
            "failures": failures,
        },
        "meta": {
            "source": "batch",
            "fetched_at": "",
            "cache_hit": False,
            "confidence": "B",
            "trace_id": trace_id(),
        },
    }
    if failures:
        payload["meta"]["error_code"] = "QFSD-8001"
        payload["meta"]["partial"] = True
        payload["meta"]["retryable"] = True
    return payload


__all__ = [
    "ALIASES",
    "LAYERS",
    "REGISTRY",
    "ToolSpec",
    "call_tool",
    "register_alias",
    "resolve",
    "run_batch",
    "tool",
    "tool_catalog",
    "tool_names",
]
