"""``th-cn`` command line (spec §12).

Two rules shape this module:

* stdout carries the requested payload (or MCP frames for ``mcp stdio``);
  logs, progress and alerts always go to stderr.
* every failure leaves through a :class:`TickHubError` so the process exits
  with the documented code (§12.4) instead of a traceback.

Commands are thin: they resolve arguments, call the same functions the MCP
server exposes, and hand the envelope to :func:`tickhub_cn.cli.output.emit`.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import importlib.util
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import typer

from .. import __version__
from ..core.config import Settings, get_settings
from ..core.errors import EXIT_CODES, TickHubError, as_error
from ..core.provenance import envelope, meta
from ..logging.setup import get_logger, setup_logging
from .output import FORMATS, emit, emit_error

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="TickHub-CN: A-share data tools over MCP (stdio / Streamable HTTP) and CLI.",
)

stock_app = typer.Typer(no_args_is_help=True, help="Stock profile commands.")
kline_app = typer.Typer(no_args_is_help=True, help="K-line commands.")
finance_app = typer.Typer(no_args_is_help=True, help="Financial statement commands.")
announcement_app = typer.Typer(no_args_is_help=True, help="Announcement commands.")
research_app = typer.Typer(no_args_is_help=True, help="Research report commands.")
risk_app = typer.Typer(no_args_is_help=True, help="Risk event commands.")
tool_app = typer.Typer(no_args_is_help=True, help="MCP tool registry commands.")
source_app = typer.Typer(no_args_is_help=True, help="Data-source health commands.")
cache_app = typer.Typer(no_args_is_help=True, help="Tiered cache commands.")
proxy_app = typer.Typer(no_args_is_help=True, help="Proxy pool commands.")
crawler_app = typer.Typer(no_args_is_help=True, help="Offline crawler commands.")
fieldmap_app = typer.Typer(no_args_is_help=True, help="Field map commands.")
config_app = typer.Typer(no_args_is_help=True, help="Configuration commands.")
mcp_app = typer.Typer(no_args_is_help=True, help="MCP transports.")
admin_app = typer.Typer(no_args_is_help=True, help="Admin console.")

for _name, _sub in {
    "stock": stock_app,
    "kline": kline_app,
    "finance": finance_app,
    "announcement": announcement_app,
    "research": research_app,
    "risk": risk_app,
    "tool": tool_app,
    "source": source_app,
    "cache": cache_app,
    "proxy": proxy_app,
    "crawler": crawler_app,
    "fieldmap": fieldmap_app,
    "config": config_app,
    "mcp": mcp_app,
    "admin": admin_app,
}.items():
    app.add_typer(_sub, name=_name)


# --------------------------------------------------------------------- state


@dataclasses.dataclass
class State:
    output: str = "json"
    fields: str = ""
    no_cache: bool = False
    refresh: bool = False
    quiet: bool = False
    verbose: bool = False


def _version_cb(value: bool) -> bool:
    if value:
        typer.echo(f"tickhub-cn {__version__}")
        raise typer.Exit()
    return value


@app.callback()
def _root(
    ctx: typer.Context,
    show_version: bool = typer.Option(
        False, "--version", callback=_version_cb, is_eager=True, help="Show the version and exit."
    ),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="json | table | csv | yaml (default json)."),
    fields: Optional[str] = typer.Option(None, "--fields", help="Comma-separated projection, e.g. code,name,price."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass the cache for this call."),
    refresh_cache: bool = typer.Option(False, "--refresh-cache", help="Refetch and overwrite the cache."),
    timeout: Optional[float] = typer.Option(None, "--timeout", help="Per-request timeout in seconds."),
    retries: Optional[int] = typer.Option(None, "--retries", help="Retry count for upstream requests."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only warnings and errors on stderr."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging on stderr."),
) -> None:
    """Global options. They may also be repeated after a subcommand."""
    fmt = (output or "json").lower()
    if fmt not in FORMATS:
        raise typer.BadParameter(f"unsupported output format: {output}")
    ctx.obj = State(output=fmt, fields=fields or "", no_cache=no_cache, refresh=refresh_cache, quiet=quiet, verbose=verbose)

    settings = get_settings()
    if timeout is not None:
        settings.http_timeout = max(0.1, float(timeout))
    if retries is not None:
        settings.http_retries = max(0, int(retries))
    if quiet:
        settings.log_level = "warning"
    elif verbose:
        settings.log_level = "debug"
    setup_logging(settings)


def _state(
    ctx: typer.Context,
    *,
    output: Optional[str] = None,
    fields: Optional[str] = None,
    no_cache: bool = False,
    refresh: bool = False,
) -> State:
    base = ctx.obj if isinstance(ctx.obj, State) else State()
    if output:
        fmt = output.lower()
        if fmt not in FORMATS:
            raise typer.BadParameter(f"unsupported output format: {output}")
        base.output = fmt
    if fields:
        base.fields = fields
    base.no_cache = base.no_cache or no_cache
    base.refresh = base.refresh or refresh
    return base


def _out_opt() -> Any:
    return typer.Option(None, "--output", "-o", help="json | table | csv | yaml (overrides the global value).")


def _fields_opt() -> Any:
    return typer.Option(None, "--fields", help="Comma-separated projection for this call.")


def _cache_opts() -> tuple[Any, Any]:
    return (
        typer.Option(False, "--no-cache", help="Bypass the cache for this call."),
        typer.Option(False, "--refresh-cache", help="Refetch and overwrite the cache."),
    )


# ------------------------------------------------------------------ plumbing


def _params(**values: Any) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None and v != ""}


def _call(state: State, name: str, **params: Any) -> Any:
    from ..datasources import call_tool

    merged = _params(**params)
    if state.fields:
        merged["fields"] = state.fields
    if state.no_cache:
        merged["_no_cache"] = True
    if state.refresh:
        merged["_refresh"] = True
    return call_tool(name, merged)


def _fail(exc: BaseException, state: State) -> None:
    if isinstance(exc, TickHubError):
        err = exc
    elif isinstance(exc, (ValueError, KeyError, TypeError)):
        err = TickHubError("QFSD-9001", str(exc) or exc.__class__.__name__)
    else:
        err = as_error(exc if isinstance(exc, Exception) else Exception(str(exc)))
    emit_error(err.to_dict(), state.output)
    raise typer.Exit(err.exit_code)


def _run(fn: Callable[[], Any], state: State) -> Any:
    try:
        payload = fn()
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        raise typer.Exit(130)
    except BrokenPipeError:
        raise typer.Exit(0)
    except BaseException as exc:  # noqa: BLE001 - mapped to a documented exit code
        _fail(exc, state)
        raise  # unreachable, keeps type checkers happy
    emit(payload, state.output)
    if isinstance(payload, dict) and payload.get("ok") is False:
        # Some tools return a partial/failed payload instead of raising so the
        # caller still gets the data (batch, crosscheck). Mirror the documented
        # exit code so scripts can branch on the result.
        code = (payload.get("meta") or {}).get("error_code")
        if code:
            raise typer.Exit(TickHubError(str(code)).exit_code)
    return payload


def _preview_params(tool: str, code: str) -> dict[str, Any]:
    from ..datasources import REGISTRY

    spec = REGISTRY.get(tool)
    if spec is None:
        return {"code": code}
    params: dict[str, Any] = {}
    for name in spec.params:
        if name in ("code", "symbol"):
            params[name] = code
        elif name == "period":
            params[name] = "daily"
        elif name == "limit":
            params[name] = 10
    return params


def _progress(state: State) -> Callable[[dict[str, Any]], None]:
    def report(event: dict[str, Any]) -> None:
        if state.quiet:
            return
        sys.stderr.write("progress " + json.dumps(event, ensure_ascii=False) + "\n")
        sys.stderr.flush()

    return report


# ------------------------------------------------------------------- top-level


@app.command("start-all")
def start_all_cmd(
    host: str = typer.Option("127.0.0.1", "--host", help="MCP HTTP bind address."),
    port: int = typer.Option(8000, "--port", help="MCP HTTP port (the only listening socket)."),
    task: str = typer.Option("all", "--task", help="Crawler task: all | announcements | reports | risks."),
    crawler: str = typer.Option("auto", "--crawler", help="auto (OFFLINE_CRAWLER_ENABLE) | on | off."),
    restart_limit: int = typer.Option(3, "--restart-limit", help="Child restarts allowed inside a 5 minute window."),
) -> None:
    """Run the MCP HTTP server and the crawler child as one supervised unit."""
    from .start_all import start_all

    raise typer.Exit(
        start_all(host=host, port=port, task=task, crawler=crawler, restart_limit=restart_limit)
    )


@app.command("doctor")
def doctor_cmd(ctx: typer.Context, output: Optional[str] = _out_opt()) -> None:
    """Check the environment, configuration, cache backend and data sources."""
    state = _state(ctx, output=output)
    _run(lambda: _doctor_payload(), state)


@app.command("version")
def version_cmd(ctx: typer.Context, output: Optional[str] = _out_opt()) -> None:
    """Print the package version."""
    state = _state(ctx, output=output)
    _run(
        lambda: envelope({"name": "tickhub-cn", "version": __version__}, source="computed", confidence="A"),
        state,
    )


# ---------------------------------------------------------------------- stock


@stock_app.command("info")
def stock_info(
    code: str = typer.Argument(..., help="6-digit A-share code."),
    output: Optional[str] = _out_opt(),
    fields: Optional[str] = _fields_opt(),
    no_cache: bool = typer.Option(False, "--no-cache"),
    refresh: bool = typer.Option(False, "--refresh-cache"),
) -> None:
    """Stock profile: name, industry, market cap, PE/PB."""
    state = _state(_ctx(), output=output, fields=fields, no_cache=no_cache, refresh=refresh)
    _run(lambda: _call(state, "stock_profile", code=code), state)


# ---------------------------------------------------------------------- kline


@kline_app.command("get")
def kline_get(
    code: str = typer.Argument(..., help="6-digit A-share code."),
    period: str = typer.Option("daily", "--period", help="daily | weekly | monthly."),
    adjust: str = typer.Option("qfq", "--adjust", help="qfq | hfq | none."),
    limit: int = typer.Option(320, "--limit", help="Number of bars (max 2000)."),
    start: str = typer.Option("", "--start", help="Start date YYYY-MM-DD."),
    end: str = typer.Option("", "--end", help="End date YYYY-MM-DD."),
    output: Optional[str] = _out_opt(),
    fields: Optional[str] = _fields_opt(),
    no_cache: bool = typer.Option(False, "--no-cache"),
    refresh: bool = typer.Option(False, "--refresh-cache"),
) -> None:
    """Daily/weekly/monthly K-line with adjustment."""
    state = _state(_ctx(), output=output, fields=fields, no_cache=no_cache, refresh=refresh)
    _run(
        lambda: _call(
            state,
            "tencent_kline",
            code=code,
            period=period,
            adjust=adjust,
            limit=limit,
            start=start,
            end=end,
        ),
        state,
    )


@kline_app.command("batch")
def kline_batch(
    codes: str = typer.Option(..., "--codes", help="Comma-separated codes (max 50)."),
    period: str = typer.Option("daily", "--period"),
    adjust: str = typer.Option("qfq", "--adjust"),
    limit: int = typer.Option(120, "--limit"),
    output: Optional[str] = _out_opt(),
    fields: Optional[str] = _fields_opt(),
    no_cache: bool = typer.Option(False, "--no-cache"),
    refresh: bool = typer.Option(False, "--refresh-cache"),
) -> None:
    """Batch K-line with per-code failure isolation."""
    state = _state(_ctx(), output=output, fields=fields, no_cache=no_cache, refresh=refresh)
    from ..datasources import call_tool, run_batch

    items = [c.strip() for c in codes.replace(";", ",").split(",") if c.strip()]

    def one(code: str) -> Any:
        params = _params(code=code, period=period, adjust=adjust, limit=limit)
        if state.fields:
            params["fields"] = state.fields
        if state.no_cache:
            params["_no_cache"] = True
        if state.refresh:
            params["_refresh"] = True
        return call_tool("tencent_kline", params)

    _run(lambda: run_batch(items, one, max_codes=50, concurrency=4, label="code"), state)


# -------------------------------------------------------------------- finance


@finance_app.command("report")
def finance_report(
    code: str = typer.Argument(..., help="6-digit A-share code."),
    granularity: str = typer.Option("full", "--granularity", help="full | income | balance | cashflow."),
    periods: int = typer.Option(8, "--periods", help="How many reporting periods."),
    report_date: str = typer.Option("", "--report-date", help="Specific report date YYYY-MM-DD."),
    output: Optional[str] = _out_opt(),
    fields: Optional[str] = _fields_opt(),
    no_cache: bool = typer.Option(False, "--no-cache"),
    refresh: bool = typer.Option(False, "--refresh-cache"),
) -> None:
    """Financial statements (income / balance / cashflow or all three)."""
    state = _state(_ctx(), output=output, fields=fields, no_cache=no_cache, refresh=refresh)
    g = (granularity or "full").strip().lower()

    def body() -> Any:
        if g in ("full", "all", "three"):
            return _call(state, "eastmoney_financial_all", code=code, periods=periods)
        statement = "cashflow" if g == "cash" else g
        if statement not in ("income", "balance", "cashflow"):
            raise TickHubError(
                "QFSD-9001",
                f"unknown granularity: {granularity} (full | income | balance | cashflow)",
            )
        return _call(
            state,
            "eastmoney_financial_report",
            code=code,
            statement=statement,
            periods=periods,
            report_date=report_date,
        )

    _run(body, state)


# --------------------------------------------------------------- announcements


@announcement_app.command("list")
def announcement_list(
    code: str = typer.Argument(..., help="6-digit A-share code."),
    type_: str = typer.Option("", "--type", help="Category keyword, e.g. 减持/增持/业绩预告."),
    limit: int = typer.Option(20, "--limit"),
    start: str = typer.Option("", "--start", help="Start date YYYY-MM-DD."),
    end: str = typer.Option("", "--end", help="End date YYYY-MM-DD."),
    page: int = typer.Option(1, "--page"),
    output: Optional[str] = _out_opt(),
    fields: Optional[str] = _fields_opt(),
    no_cache: bool = typer.Option(False, "--no-cache"),
    refresh: bool = typer.Option(False, "--refresh-cache"),
) -> None:
    """CNINFO announcements with PDF links."""
    state = _state(_ctx(), output=output, fields=fields, no_cache=no_cache, refresh=refresh)
    _run(
        lambda: _call(
            state,
            "cninfo_announcements",
            code=code,
            start=start,
            end=end,
            category=type_,
            limit=limit,
            page=page,
        ),
        state,
    )


# ------------------------------------------------------------------- research


@research_app.command("list")
def research_list(
    code: str = typer.Argument(..., help="6-digit A-share code."),
    limit: int = typer.Option(10, "--limit"),
    page: int = typer.Option(1, "--page"),
    output: Optional[str] = _out_opt(),
    fields: Optional[str] = _fields_opt(),
    no_cache: bool = typer.Option(False, "--no-cache"),
    refresh: bool = typer.Option(False, "--refresh-cache"),
) -> None:
    """Broker research reports with rating and PDF links."""
    state = _state(_ctx(), output=output, fields=fields, no_cache=no_cache, refresh=refresh)
    _run(
        lambda: _call(state, "eastmoney_reports", code=code, page=page, page_size=limit),
        state,
    )


# ----------------------------------------------------------------------- risk


@risk_app.command("events")
def risk_events(
    code: str = typer.Argument(..., help="6-digit A-share code."),
    days: int = typer.Option(90, "--days", help="Look-back window in days."),
    limit: int = typer.Option(100, "--limit"),
    archive: bool = typer.Option(False, "--archive", help="Read risk events already stored locally."),
    output: Optional[str] = _out_opt(),
    fields: Optional[str] = _fields_opt(),
    no_cache: bool = typer.Option(False, "--no-cache"),
    refresh: bool = typer.Option(False, "--refresh-cache"),
) -> None:
    """Structured risk events derived from announcements."""
    state = _state(_ctx(), output=output, fields=fields, no_cache=no_cache, refresh=refresh)

    def body() -> Any:
        if archive:
            from ..crawler.store import get_archive

            events = get_archive().risks(code=code, limit=limit)
            return envelope(
                {"code": code, "events": events, "total": len(events)},
                source="computed",
                extra={"origin": "archive"},
            )
        end = _dt.date.today()
        start = end - _dt.timedelta(days=max(1, int(days)))
        return _call(
            state,
            "cninfo_announcement_risk",
            code=code,
            start=start.isoformat(),
            end=end.isoformat(),
            limit=limit,
        )

    _run(body, state)


# ----------------------------------------------------------------------- tool


@tool_app.command("list")
def tool_list(
    layer: str = typer.Option("", "--layer", help="Filter by data layer."),
    aliases: bool = typer.Option(True, "--aliases/--no-aliases", help="Include compatibility aliases."),
    output: Optional[str] = _out_opt(),
) -> None:
    """List every registered MCP tool."""
    state = _state(_ctx(), output=output)
    from ..datasources import ALIASES, tool_catalog

    def body() -> Any:
        items = tool_catalog()
        if aliases:
            for alias in sorted(ALIASES):
                target = ALIASES[alias]
                items.append(
                    {
                        "name": alias,
                        "layer": "alias",
                        "description": f"compatibility alias of {target}",
                        "ttl_seconds": None,
                        "params": [],
                    }
                )
        if layer:
            items = [i for i in items if i.get("layer") == layer]
        return envelope(
            items,
            source="computed",
            confidence="A",
            extra={"count": len(items), "registered": len(tool_catalog()), "aliases": len(ALIASES) if aliases else 0},
        )

    _run(body, state)


@tool_app.command("call")
def tool_call(
    name: str = typer.Argument(..., help="Tool name (aliases accepted)."),
    params: str = typer.Option("{}", "--params", help="JSON object of tool arguments."),
    output: Optional[str] = _out_opt(),
    fields: Optional[str] = _fields_opt(),
    no_cache: bool = typer.Option(False, "--no-cache"),
    refresh: bool = typer.Option(False, "--refresh-cache"),
) -> None:
    """Call any tool by name."""
    state = _state(_ctx(), output=output, fields=fields, no_cache=no_cache, refresh=refresh)

    def body() -> Any:
        try:
            parsed = json.loads(params or "{}")
        except json.JSONDecodeError as exc:
            raise TickHubError("QFSD-9001", f"invalid --params JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise TickHubError("QFSD-9001", "--params must be a JSON object")
        return _call(state, name, **parsed)

    _run(body, state)


# --------------------------------------------------------------------- source


@source_app.command("health")
def source_health(ctx: typer.Context, output: Optional[str] = _out_opt()) -> None:
    """Per-source health from live accounting and the shared cache."""
    state = _state(ctx, output=output)
    _run(lambda: _health_payload(), state)


def _health_payload() -> dict[str, Any]:
    from ..cache.redis_cache import get_cache
    from ..core.http import HEALTH, SOURCE_LIMITS

    live = HEALTH.snapshot()
    try:
        stored = get_cache().read_health()
    except Exception:
        stored = {}
    rows: list[dict[str, Any]] = []
    for name in sorted(set(SOURCE_LIMITS) | set(live) | set(stored)):
        row: dict[str, Any] = {
            "source": name,
            "ok": True,
            "successes": 0,
            "failures": 0,
            "last_latency_ms": None,
            "last_checked_at": None,
            "last_error": None,
        }
        row.update(stored.get(name) or {})
        row.update(live.get(name) or {})
        rows.append(row)
    return envelope(
        {"sources": rows, "count": len(rows)},
        source="computed",
        extra={"observed": len(live)},
    )


# ---------------------------------------------------------------------- cache


@cache_app.command("stats")
def cache_stats(ctx: typer.Context, output: Optional[str] = _out_opt()) -> None:
    """Hit/miss counters and the configured TTL tiers."""
    state = _state(ctx, output=output)

    def body() -> Any:
        from ..cache.redis_cache import TTL, get_cache

        return envelope({"stats": get_cache().stats(), "ttl_seconds": TTL}, source="cache")

    _run(body, state)


@cache_app.command("refresh")
def cache_refresh(
    ctx: typer.Context,
    domain: str = typer.Option(..., "--domain", help="Cache domain, e.g. kline."),
    code: str = typer.Option("", "--code", help="Optional code to narrow the scope."),
    tool: str = typer.Option("", "--tool", help="Optional tool to re-warm after invalidating."),
    output: Optional[str] = _out_opt(),
) -> None:
    """Invalidate cached entries so the next query goes live (optionally re-warm)."""
    state = _state(ctx, output=output)
    from ..cache.redis_cache import get_cache

    def body() -> Any:
        prefix = f"{domain}:{code}" if code else domain
        removed = get_cache().flush(prefix)
        data: dict[str, Any] = {"domain": domain, "code": code or None, "removed": removed}
        if tool:
            data["warm"] = _call(state, tool, code=code, _refresh=True)
        return envelope(data, source="cache")

    _run(body, state)


@cache_app.command("flush")
def cache_flush(
    ctx: typer.Context,
    prefix: str = typer.Option("", "--prefix", help="Key prefix (domain[:code]); empty flushes everything."),
    output: Optional[str] = _out_opt(),
) -> None:
    """Drop cached entries."""
    state = _state(ctx, output=output)
    from ..cache.redis_cache import get_cache

    _run(lambda: envelope({"prefix": prefix, "removed": get_cache().flush(prefix or None)}, source="cache"), state)


# ---------------------------------------------------------------------- proxy


@proxy_app.command("list")
def proxy_list(ctx: typer.Context, output: Optional[str] = _out_opt()) -> None:
    """List configured proxy nodes with credentials masked."""
    state = _state(ctx, output=output)
    from ..proxy.pool import get_pool

    def body() -> Any:
        pool = get_pool()
        nodes = pool.masked()
        return envelope({"enabled": bool(nodes), "nodes": nodes, "count": len(nodes)}, source="computed")

    _run(body, state)


@proxy_app.command("check")
def proxy_check(
    ctx: typer.Context,
    timeout: int = typer.Option(5, "--timeout-seconds", help="Per-node timeout."),
    output: Optional[str] = _out_opt(),
) -> None:
    """Probe every proxy node and report latency."""
    state = _state(ctx, output=output)
    from ..proxy.pool import get_pool

    _run(lambda: envelope({"nodes": get_pool().check_all(timeout)}, source="computed"), state)


# -------------------------------------------------------------------- crawler


@crawler_app.command("status")
def crawler_status(ctx: typer.Context, output: Optional[str] = _out_opt()) -> None:
    """Archive statistics, queue depth and daemon state."""
    state = _state(ctx, output=output)

    def body() -> Any:
        from ..crawler import daemon_status

        return envelope(daemon_status(), source="computed")

    _run(body, state)


@crawler_app.command("run")
def crawler_run(
    ctx: typer.Context,
    task: str = typer.Option("all", "--task", help="all | announcements | reports | risks."),
    codes: str = typer.Option("", "--codes", help="Comma-separated codes; defaults to the watchlist."),
    days: int = typer.Option(7, "--days", help="Look-back window in days."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Codes per run."),
    concurrency: Optional[int] = typer.Option(None, "--concurrency"),
    output: Optional[str] = _out_opt(),
) -> None:
    """Run one crawl task now."""
    state = _state(ctx, output=output)
    from ..crawler import run_task

    payload = _run(
        lambda: envelope(
            run_task(
                task,
                codes=codes or None,
                days=days,
                limit=limit,
                concurrency=concurrency,
                progress=_progress(state),
            ),
            source="crawler",
        ),
        state,
    )
    result = payload.get("data") or {}
    if result.get("status") == "failed":
        raise typer.Exit(EXIT_CODES["source_unavailable"])
    if result.get("status") == "partial":
        raise typer.Exit(EXIT_CODES["batch_partial_failure"])


@crawler_app.command("daemon")
def crawler_daemon(
    task: str = typer.Option("all", "--task", help="Task the cron job runs."),
    force: bool = typer.Option(False, "--force", help="Run even when OFFLINE_CRAWLER_ENABLE is false."),
    run_on_start: Optional[bool] = typer.Option(
        None, "--run-on-start/--no-run-on-start", help="Override OFFLINE_CRAWLER_RUN_ON_START."
    ),
) -> None:
    """Run the blocking cron crawler. No network port is opened."""
    from ..crawler import run_daemon

    try:
        code = run_daemon(force=force, run_now=run_on_start, task=task)
    except KeyboardInterrupt:
        code = 0
    raise typer.Exit(code)


# ------------------------------------------------------------------- fieldmap


@fieldmap_app.command("validate")
def fieldmap_validate(ctx: typer.Context, output: Optional[str] = _out_opt()) -> None:
    """Validate field_map.toml."""
    state = _state(ctx, output=output)

    def body() -> Any:
        from ..fieldmap.loader import FieldMap

        settings = get_settings()
        path = Path(settings.field_map_path)
        fm = FieldMap.load(path)
        data = {
            "path": str(path),
            "exists": path.exists(),
            "valid": fm.error is None,
            "error": fm.error,
            "tools": sorted(fm.tools),
        }
        return envelope(data, source="computed", extra={"count": len(fm.tools)})

    payload = _run(body, state)
    if (payload.get("data") or {}).get("valid") is False:
        raise typer.Exit(EXIT_CODES["config_error"])


@fieldmap_app.command("preview")
def fieldmap_preview(
    ctx: typer.Context,
    tool: str = typer.Option("get_finance_report", "--tool", help="Tool name."),
    code: str = typer.Option("600000", "--code", help="Code used for the sample call."),
    output: Optional[str] = _out_opt(),
    fields: Optional[str] = _fields_opt(),
) -> None:
    """Show the projection a tool response would get."""
    state = _state(ctx, output=output, fields=fields)

    def body() -> Any:
        params = _preview_params(tool, code)
        payload = _call(state, tool, **params)
        return envelope(
            {"tool": tool, "params": params, "payload": payload},
            source="computed",
        )

    _run(body, state)


# --------------------------------------------------------------------- config


@config_app.command("check")
def config_check(ctx: typer.Context, output: Optional[str] = _out_opt()) -> None:
    """Validate the effective configuration."""
    state = _state(ctx, output=output)
    payload = _run(lambda: _config_check_payload(), state)
    if (payload.get("data") or {}).get("status") == "error":
        raise typer.Exit(EXIT_CODES["config_error"])


@config_app.command("show")
def config_show(
    ctx: typer.Context,
    mask_sensitive: bool = typer.Option(
        True, "--mask-sensitive/--no-mask-sensitive", help="Mask keys/tokens (default: on)."
    ),
    output: Optional[str] = _out_opt(),
) -> None:
    """Print the effective settings."""
    state = _state(ctx, output=output)

    def body() -> Any:
        settings = get_settings()
        if mask_sensitive:
            data = settings.masked()
        else:
            data = {f.name: getattr(settings, f.name) for f in dataclasses.fields(settings) if f.name != "extra"}
        return envelope(data, source="computed", extra={"masked": mask_sensitive})

    _run(body, state)


def _config_check_payload() -> dict[str, Any]:
    settings = get_settings()
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str = "") -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    add("python", "ok" if sys.version_info >= (3, 11) else "error", platform.python_version())

    if settings.run_mode in ("stdio", "http", "start-all", "cli"):
        add("run_mode", "ok", settings.run_mode)
    else:
        add("run_mode", "error", f"unexpected run_mode: {settings.run_mode}")

    if settings.log_level.lower() not in ("debug", "info", "warning", "error", "critical"):
        add("log_level", "error", settings.log_level)
    else:
        add("log_level", "ok", settings.log_level)

    scheme = (settings.redis_url or "").split(":", 1)[0].lower()
    if not settings.redis_url:
        add("redis_url", "warn", "empty: the in-process cache will be used")
    elif scheme in ("redis", "rediss", "unix"):
        add("redis_url", "ok", f"{scheme}://...")
    else:
        add("redis_url", "error", f"unsupported scheme: {scheme}")

    archive = Path(settings.archive_dir)
    try:
        archive.mkdir(parents=True, exist_ok=True)
        add("archive_dir", "ok" if os.access(archive, os.W_OK) else "error", str(archive))
    except Exception as exc:  # noqa: BLE001
        add("archive_dir", "error", f"{archive}: {exc}")

    from ..fieldmap.loader import FieldMap

    fm = FieldMap.load(settings.field_map_path)
    add("field_map", "error" if fm.error else "ok", fm.error or f"{len(fm.tools)} tools mapped")

    if settings.proxy_pool_config_path and not Path(settings.proxy_pool_config_path).exists():
        add("proxy_pool", "warn", f"missing file: {settings.proxy_pool_config_path}")
    else:
        add("proxy_pool", "ok", settings.proxy_pool_config_path or "inline list / disabled")

    if settings.admin_host not in ("127.0.0.1", "::1", "localhost") and not settings.admin_auth_token:
        add("admin", "error", "non-loopback admin host requires ADMIN_AUTH_TOKEN")
    else:
        add("admin", "ok", f"{settings.admin_host}:{settings.admin_port}")

    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(settings.offline_crawler_cron)
        add("crawler_cron", "ok", settings.offline_crawler_cron)
    except Exception as exc:  # noqa: BLE001
        add("crawler_cron", "error", f"{settings.offline_crawler_cron}: {exc}")

    status = _overall(checks)
    return envelope(
        {"status": status, "checks": checks, "errors": sum(c["status"] == "error" for c in checks)},
        source="computed",
        confidence="A",
    )


def _overall(checks: list[dict[str, Any]]) -> str:
    if any(c["status"] == "error" for c in checks):
        return "error"
    if any(c["status"] == "warn" for c in checks):
        return "degraded"
    return "ok"


def _doctor_payload() -> dict[str, Any]:
    settings = get_settings()
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str = "") -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    add("python", "ok" if sys.version_info >= (3, 11) else "error", platform.python_version())
    add("version", "ok", __version__)

    for module, label, required in (
        ("mcp", "mcp", True),
        ("requests", "requests", True),
        ("typer", "typer", True),
        ("toml", "toml", False),
        ("dotenv", "python-dotenv", False),
        ("redis", "redis", False),
        ("apscheduler", "apscheduler", False),
        ("uvicorn", "uvicorn", False),
        ("fastapi", "fastapi", False),
    ):
        present = importlib.util.find_spec(module) is not None
        add(f"dep:{label}", "ok" if present else ("error" if required else "warn"), "installed" if present else "missing")

    try:
        from ..cache.redis_cache import get_cache

        stats = get_cache().stats()
        add("cache", "ok" if stats.get("backend") == "redis" else "warn", f"backend={stats.get('backend')}")
    except Exception as exc:  # noqa: BLE001
        add("cache", "error", str(exc)[:200])

    archive = Path(settings.archive_dir)
    try:
        archive.mkdir(parents=True, exist_ok=True)
        add("archive", "ok" if os.access(archive, os.W_OK) else "error", str(archive))
    except Exception as exc:  # noqa: BLE001
        add("archive", "error", str(exc)[:200])

    try:
        from ..crawler.store import get_archive

        stats = get_archive().stats()
        add("archive_db", "ok", f"announcements={stats.get('announcements', 0)} risks={stats.get('risks', 0)}")
    except Exception as exc:  # noqa: BLE001
        add("archive_db", "warn", str(exc)[:200])

    try:
        from ..fieldmap.loader import FieldMap

        fm = FieldMap.load(settings.field_map_path)
        add("field_map", "error" if fm.error else "ok", fm.error or f"{len(fm.tools)} tools mapped")
    except Exception as exc:  # noqa: BLE001
        add("field_map", "warn", str(exc)[:200])

    try:
        from ..proxy.pool import get_pool

        nodes = get_pool().masked()
        add("proxy", "ok", f"{len(nodes)} node(s)")
    except Exception as exc:  # noqa: BLE001
        add("proxy", "warn", str(exc)[:200])

    try:
        from ..datasources import ALIASES, REGISTRY

        add("tools", "ok", f"{len(REGISTRY)} tools + {len(ALIASES)} aliases")
    except Exception as exc:  # noqa: BLE001
        add("tools", "error", str(exc)[:200])

    health = _health_payload()
    sources = (health.get("data") or {}).get("sources") or []
    failing = [s["source"] for s in sources if not s.get("ok", True)]
    add("sources", "warn" if failing else "ok", ", ".join(failing) if failing else "no failures recorded")

    status = _overall(checks)
    return envelope(
        {
            "status": status,
            "version": __version__,
            "run_mode": settings.run_mode,
            "cache_backend": _cache_backend(),
            "checks": checks,
            "sources": sources,
            "advice": "run `th-cn config check`, then `th-cn source health` if a source keeps failing",
        },
        source="computed",
        confidence="A",
    )


def _cache_backend() -> str:
    try:
        from ..cache.redis_cache import get_cache

        return str(get_cache().stats().get("backend") or "unknown")
    except Exception:
        return "unknown"


# ------------------------------------------------------------------------ mcp


@mcp_app.command("stdio")
def mcp_stdio() -> None:
    """Serve MCP over stdio. stdout carries protocol frames only."""
    from ..mcp.server import run_stdio

    try:
        run_stdio()
    except KeyboardInterrupt:
        raise typer.Exit(0)
    except Exception as exc:  # noqa: BLE001 - never write to stdout here
        get_logger("cli").error(
            "stdio server failed",
            extra={"event": "mcp_stdio_failed", "error_code": "QFSD-9003", "error": str(exc)[:200]},
        )
        raise typer.Exit(EXIT_CODES["source_unavailable"])
    raise typer.Exit(0)


@mcp_app.command("http")
def mcp_http(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    path: str = typer.Option("/mcp", "--path", help="Streamable HTTP mount path."),
) -> None:
    """Serve MCP over Streamable HTTP (default 127.0.0.1:8000/mcp)."""
    from ..mcp.server import run_http

    try:
        run_http(host=host, port=port, path=path)
    except KeyboardInterrupt:
        raise typer.Exit(0)
    except Exception as exc:  # noqa: BLE001
        get_logger("cli").error(
            "http server failed",
            extra={"event": "mcp_http_failed", "error_code": "QFSD-9003", "error": str(exc)[:200]},
        )
        raise typer.Exit(EXIT_CODES["source_unavailable"])
    raise typer.Exit(0)


# ---------------------------------------------------------------------- admin


@admin_app.command("serve")
def admin_serve(
    host: Optional[str] = typer.Option(None, "--host", help="Bind address (default from settings)."),
    port: Optional[int] = typer.Option(None, "--port", help="Port (default 8787)."),
    force: bool = typer.Option(False, "--force", help="Start even when ADMIN_ENABLE is false."),
) -> None:
    """Run the admin console in its own process."""
    from ..admin_api.app import serve

    raise typer.Exit(serve(host, port, force=force))


# ----------------------------------------------------------------------- main


def _ctx() -> typer.Context:
    """Current context for leaf commands defined without one.

    Typer injects ``ctx`` only when the parameter is declared; declaring it in
    every data command adds noise, so leaf handlers fetch it from the context
    stack instead. Newer Typer releases vendor their own Click, so the bare
    ``click`` package may not hold this app's stack.
    """
    try:
        from typer._click.globals import get_current_context
    except ImportError:  # pragma: no cover - Typer built on upstream Click
        from click.globals import get_current_context

    return get_current_context()  # type: ignore[return-value]


def main() -> None:
    # JSON output must stay UTF-8 even on a GBK Windows console or when the
    # stream is redirected to a file.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    app()


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["app", "main"]
