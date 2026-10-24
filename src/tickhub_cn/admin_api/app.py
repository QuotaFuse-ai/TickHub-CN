"""Admin console API (spec §4.14).

Runs as its own process (``th-cn admin serve``), never inside the MCP server,
so it cannot disturb MCP framing. Defaults: off, 127.0.0.1:8787, token auth for
anything non-loopback, every write audited, secrets masked.
"""

# No ``from __future__ import annotations`` here: the route factories import
# FastAPI lazily, and stringified annotations would make ``request: Request``
# unresolvable (FastAPI would then read it as a required query parameter).

import json
import os
import sqlite3
import sys
import time
from collections import deque
from dataclasses import fields as dataclass_fields
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..cache.redis_cache import get_cache
from ..core.config import SECRET_HINTS, Settings, get_settings
from ..core.http import HEALTH, read_health_state
from ..logging.setup import audit, get_logger
from ..proxy.pool import get_pool, reset_pool

try:
    import toml
except Exception:  # pragma: no cover
    toml = None

ADMIN_CONFIG_PATH = "./config/admin.toml"
_STARTED = time.time()

TOML_KEYS = {
    "enable": "admin_enable",
    "host": "admin_host",
    "port": "admin_port",
    "auth_token": "admin_auth_token",
    "readonly": "admin_readonly",
    "cors_origins": "admin_cors_origins",
    "audit_sink": "admin_audit_sink",
    "sensitive_mask": "admin_sensitive_mask",
}


def _log() -> Any:
    return get_logger("admin")


def read_admin_toml(path: str | Path = ADMIN_CONFIG_PATH) -> dict[str, Any]:
    p = Path(path)
    if not p.exists() or toml is None:
        return {}
    try:
        return dict(toml.loads(p.read_text(encoding="utf-8")).get("admin") or {})
    except Exception as exc:
        _log().warning("admin.toml invalid", extra={"error": str(exc)[:200], "error_code": "QFSD-3001"})
        return {}


def effective_admin(settings: Settings | None = None) -> dict[str, Any]:
    """admin.toml is the base; an explicit ADMIN_* env var always wins."""
    s = settings or get_settings()
    out: dict[str, Any] = {key: getattr(s, attr) for key, attr in TOML_KEYS.items()}
    for key, value in read_admin_toml().items():
        attr = TOML_KEYS.get(key)
        if not attr or os.environ.get(f"ADMIN_{key.upper()}"):
            continue
        out[key] = value
    out["port"] = int(out.get("port") or 8787)
    out["enable"] = bool(out.get("enable"))
    out["readonly"] = bool(out.get("readonly"))
    out["sensitive_mask"] = bool(out.get("sensitive_mask", True))
    out["web_dir"] = s.admin_web_dir
    return out


def masked_settings(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    out: dict[str, Any] = {}
    for f in dataclass_fields(s):
        value = getattr(s, f.name)
        if any(hint in f.name.lower() for hint in SECRET_HINTS) and value:
            value = "***"
        out[f.name] = value
    return out


def _client_host(request: Any) -> str:
    try:
        return str(request.client.host if request.client else "")
    except Exception:
        return ""


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


def create_app(settings: Settings | None = None, *, admin: dict[str, Any] | None = None) -> Any:
    from fastapi import Body, FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

    settings = settings or get_settings()
    admin = dict(admin or effective_admin(settings))
    token = str(admin.get("auth_token") or "")
    readonly = bool(admin.get("readonly"))

    app = FastAPI(title="TickHub-CN Admin", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.state.admin = admin
    app.state.settings = settings

    raw_origins = admin.get("cors_origins") or ""
    if isinstance(raw_origins, (list, tuple)):
        origins = [str(o).strip() for o in raw_origins if str(o).strip()]
    else:
        origins = [o.strip() for o in str(raw_origins).split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "X-Admin-Token", "Content-Type"],
        )

    def auth(request: Request) -> str:
        if token:
            supplied = request.headers.get("x-admin-token", "")
            header = request.headers.get("authorization", "")
            if not supplied and header.lower().startswith("bearer "):
                supplied = header[7:].strip()
            if supplied != token:
                _log().warning(
                    "admin auth rejected",
                    extra={"event": "admin_auth_failed", "error_code": "QFSD-6001", "detail": {"path": request.url.path}},
                )
                raise HTTPException(status_code=401, detail="invalid admin token")
        elif not _is_loopback(_client_host(request)):
            raise HTTPException(status_code=401, detail="set ADMIN_AUTH_TOKEN before exposing the console")
        return "token" if token else "loopback"

    def write_guard(actor: str) -> None:
        if readonly:
            raise HTTPException(status_code=403, detail="admin console is read-only (ADMIN_READONLY=true)")

    @app.exception_handler(Exception)
    def _unhandled(_request: Request, exc: Exception) -> Any:  # pragma: no cover - safety net
        _log().error("admin request failed", extra={"event": "admin_error", "error": str(exc)[:300]})
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)[:300]})

    # ------------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> Any:
        page = resolve_web_dir(admin.get("web_dir") or settings.admin_web_dir) / "index.html"
        if page.exists():
            return FileResponse(page)
        return HTMLResponse(
            "<h1>TickHub-CN Admin API</h1>"
            "<p>UI files not found. API lives under <code>/api/*</code>; "
            "see <a href='/api/docs'>/api/docs</a>.</p>"
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    # ------------------------------------------------------------- dashboard

    @app.get("/api/status")
    def status(request: Request) -> dict[str, Any]:
        actor = auth(request)
        from ..crawler import crawl_status
        from ..datasources import LAYERS, REGISTRY

        cache_stats: dict[str, Any]
        try:
            cache_stats = get_cache().stats()
        except Exception as exc:
            cache_stats = {"error": str(exc)[:200]}
        try:
            crawler = crawl_status()
        except Exception as exc:
            crawler = {"error": str(exc)[:200]}
        return {
            "ok": True,
            "version": __version__,
            "uptime_s": round(time.time() - _STARTED, 1),
            "auth": actor,
            "readonly": readonly,
            "tools": len(REGISTRY),
            "layers": {k: len(v) for k, v in LAYERS.items()},
            "cache": cache_stats,
            "crawler": crawler,
            "health": _health_snapshot(),
        }

    @app.get("/api/sources")
    def sources(request: Request) -> dict[str, Any]:
        auth(request)
        from ..datasources import LAYERS

        health = _health_snapshot()
        return {"ok": True, "layers": LAYERS, "health": health}

    @app.post("/api/sources/check")
    def sources_check(request: Request) -> dict[str, Any]:
        actor = auth(request)
        write_guard(actor)
        snapshot = _health_snapshot()
        audit("sources.check", actor=actor, detail={"sources": len(snapshot)})
        return {"ok": True, "health": snapshot, "note": "health comes from live request accounting"}

    # ------------------------------------------------------------------ cache

    @app.get("/api/cache/stats")
    def cache_stats(request: Request) -> dict[str, Any]:
        auth(request)
        return {"ok": True, **get_cache().stats()}

    @app.post("/api/cache/flush")
    def cache_flush(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        actor = auth(request)
        write_guard(actor)
        prefix = str(payload.get("prefix") or payload.get("domain") or "").strip()
        removed = get_cache().flush(prefix or None)
        audit("cache.flush", actor=actor, detail={"prefix": prefix, "removed": removed})
        return {"ok": True, "prefix": prefix, "removed": removed}

    # ------------------------------------------------------------------ proxy

    @app.get("/api/proxy")
    def proxy_list(request: Request) -> dict[str, Any]:
        auth(request)
        pool = get_pool()
        return {"ok": True, "enabled": bool(pool), "nodes": pool.masked()}

    @app.post("/api/proxy/check")
    def proxy_check(request: Request) -> dict[str, Any]:
        actor = auth(request)
        write_guard(actor)
        nodes = get_pool().check_all()
        audit("proxy.check", actor=actor, detail={"nodes": len(nodes)})
        return {"ok": True, "nodes": nodes}

    @app.post("/api/proxy/reload")
    def proxy_reload(request: Request) -> dict[str, Any]:
        actor = auth(request)
        write_guard(actor)
        reset_pool()
        pool = get_pool()
        audit("proxy.reload", actor=actor, detail={"nodes": len(pool.masked())})
        return {"ok": True, "nodes": pool.masked()}

    # --------------------------------------------------------------- fieldmap

    @app.get("/api/fieldmap")
    def fieldmap_get(request: Request) -> dict[str, Any]:
        auth(request)
        path = Path(settings.field_map_path)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return {"ok": True, "path": str(path), "content": text, "exists": path.exists()}

    @app.put("/api/fieldmap")
    def fieldmap_put(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        actor = auth(request)
        write_guard(actor)
        content = str(payload.get("content") or "")
        parsed = _validate_fieldmap(content)
        if parsed.get("error"):
            raise HTTPException(status_code=422, detail=parsed["error"])
        path = Path(settings.field_map_path)
        if path.exists():
            path.with_suffix(path.suffix + ".bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(content, encoding="utf-8")
        from ..fieldmap.loader import reload_field_map

        reload_field_map()
        audit("fieldmap.save", actor=actor, detail={"tools": len(parsed.get("tools", [])), "bytes": len(content)})
        return {"ok": True, "tools": parsed.get("tools", [])}

    @app.get("/api/fieldmap/preview")
    def fieldmap_preview(request: Request, tool: str = "get_kline", code: str = "600000") -> dict[str, Any]:
        auth(request)
        from ..datasources import call_tool

        params = _preview_params(tool, code)
        payload = call_tool(tool, params)
        return {"ok": True, "tool": tool, "params": params, "payload": payload}

    # ------------------------------------------------------------------- logs

    @app.get("/api/logs")
    def logs(request: Request, limit: int = 100, level: str = "", q: str = "") -> dict[str, Any]:
        auth(request)
        rows, origin = _read_logs(settings, limit=limit, level=level, q=q)
        return {"ok": True, "rows": rows, "source": origin, "path": settings.log_file}

    # -------------------------------------------------------------- mcp debug

    @app.get("/api/mcp/tools")
    def mcp_tools(request: Request, layer: str = "") -> dict[str, Any]:
        auth(request)
        from ..datasources import REGISTRY, tool_catalog

        items = tool_catalog()
        if layer:
            items = [i for i in items if i["layer"] == layer]
        return {"ok": True, "count": len(REGISTRY), "tools": items}

    @app.post("/api/mcp/call")
    def mcp_call(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        actor = auth(request)
        write_guard(actor)
        from ..core.errors import TickHubError
        from ..datasources import call_tool

        name = str(payload.get("tool") or payload.get("name") or "").strip()
        params = payload.get("params") or {}
        if not name or not isinstance(params, dict):
            raise HTTPException(status_code=422, detail="tool and params are required")
        try:
            result = call_tool(name, params)
        except TickHubError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": exc.to_dict()})
        audit("mcp.call", actor=actor, detail={"tool": name})
        return {"ok": True, "tool": name, "payload": result}

    # ---------------------------------------------------------------- crawler

    @app.get("/api/crawler/status")
    def crawler_status_api(request: Request) -> dict[str, Any]:
        auth(request)
        from ..crawler import daemon_status

        return {"ok": True, **daemon_status()}

    @app.post("/api/crawler/run")
    def crawler_run(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        actor = auth(request)
        write_guard(actor)
        from ..crawler import run_task

        task = str(payload.get("task") or "all")
        codes = payload.get("codes")
        result = run_task(task, codes=codes, days=int(payload.get("days") or 7))
        audit("crawler.run", actor=actor, detail={"task": task, "status": result.get("status")})
        return {"ok": True, **result}

    @app.get("/api/queue")
    def queue(request: Request) -> dict[str, Any]:
        auth(request)
        from ..crawler import TASKS

        cache = get_cache()
        return {"ok": True, "queue": {task: cache.queue_length(task) for task in TASKS}}

    # ------------------------------------------------------- P1 read surfaces

    @app.get("/api/crosscheck")
    def crosscheck_api(request: Request, code: str, report_date: str = "") -> dict[str, Any]:
        auth(request)
        from ..datasources import call_tool

        return {"ok": True, "payload": call_tool("finance_crosscheck", {"code": code, "report_date": report_date})}

    @app.get("/api/risk")
    def risk_api(request: Request, code: str = "", limit: int = 100) -> dict[str, Any]:
        auth(request)
        from ..crawler.store import get_archive

        return {"ok": True, "events": get_archive().risks(code=code, limit=limit)}

    @app.get("/api/config")
    def config_api(request: Request) -> dict[str, Any]:
        auth(request)
        data = masked_settings(settings)
        return {"ok": True, "config": data, "masked": True}

    @app.get("/api/audit")
    def audit_api(request: Request, limit: int = 100) -> dict[str, Any]:
        auth(request)
        return {"ok": True, "rows": _read_audit(limit=limit)}

    return app


def _checked_at(payload: dict[str, Any]) -> float:
    raw = str(payload.get("last_checked_at") or "")
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return 0.0


def _health_snapshot() -> dict[str, Any]:
    """Merge health from disk (other processes), Redis and this process.

    Three writers can cover the same source; the most recently checked wins so
    a stale local counter never hides a fresh observation from the MCP server.
    """
    merged: dict[str, Any] = {}
    sources = [read_health_state()]
    try:
        sources.append(get_cache().read_health())
    except Exception:
        pass
    sources.append(HEALTH.snapshot())
    for batch in sources:
        for source, payload in batch.items():
            current = merged.get(source)
            if current is None or _checked_at(payload) >= _checked_at(current):
                merged[source] = payload
    return merged


def resolve_web_dir(configured: str | Path = "./web/admin") -> Path:
    """Locate the console page in a checkout, an editable install or a wheel."""
    p = Path(configured)
    if (p / "index.html").exists():
        return p
    for candidate in (
        Path(__file__).resolve().parents[3] / "web" / "admin",
        Path(sys.prefix) / "share" / "tickhub-cn" / "web" / "admin",
    ):
        if (candidate / "index.html").exists():
            return candidate
    return p


def _validate_fieldmap(content: str) -> dict[str, Any]:
    if toml is None:
        return {"error": "toml library missing"}
    try:
        raw = toml.loads(content)
    except Exception as exc:
        return {"error": f"invalid TOML: {exc}"}
    tools = raw.get("tools") or {}
    if not isinstance(tools, dict):
        return {"error": "tools must be a table"}
    for name, conf in tools.items():
        if not isinstance(conf, dict):
            return {"error": f"tool {name} must be a table"}
        for key in ("alias",):
            if key in conf and not isinstance(conf[key], dict):
                return {"error": f"tools.{name}.alias must be a table"}
        for key in ("hide", "default_fields"):
            if key in conf and not isinstance(conf[key], list):
                return {"error": f"tools.{name}.{key} must be a list"}
    return {"tools": sorted(tools)}


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


def _read_logs(
    settings: Settings, *, limit: int, level: str, q: str
) -> tuple[list[dict[str, Any]], str]:
    cap = max(1, min(int(limit), 500))
    db = Path(settings.log_file + ".db")
    if db.exists():
        return _read_sqlite_logs(db, limit=cap, level=level, q=q), "sqlite"
    return _read_jsonl_logs(Path(settings.log_file), limit=cap, level=level, q=q), "jsonl"


def _read_sqlite_logs(path: Path, *, limit: int, level: str, q: str) -> list[dict[str, Any]]:
    sql = "SELECT ts, level, logger, message, payload FROM log_entry"
    clauses: list[str] = []
    params: list[Any] = []
    if level:
        clauses.append("level = ?")
        params.append(level.lower())
    if q:
        clauses.append("(message LIKE ? OR payload LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    try:
        con = sqlite3.connect(path, timeout=3)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()
    except Exception as exc:
        _log().warning("log query failed", extra={"error": str(exc)[:200]})
        return []


def _read_jsonl_logs(path: Path, *, limit: int, level: str, q: str) -> list[dict[str, Any]]:
    """Tail the JSON-lines file every sink mirrors to; newest first."""
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            tail = deque(fh, maxlen=max(limit * 4, 200))
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if level and str(entry.get("level") or "").lower() != level.lower():
            continue
        message = str(entry.get("message") or "")
        payload = json.dumps(entry, ensure_ascii=False)
        if q and q not in message and q not in payload:
            continue
        rows.append(
            {
                "ts": entry.get("ts"),
                "level": entry.get("level"),
                "logger": entry.get("logger"),
                "message": message,
                "payload": payload,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _read_audit(limit: int = 100) -> list[dict[str, Any]]:
    path = Path("./logs/admin_audit.jsonl")
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-max(1, min(int(limit), 500)):]
        for line in reversed(lines):
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return rows


def serve(host: str | None = None, port: int | None = None, *, force: bool = False) -> int:
    settings = get_settings()
    admin = effective_admin(settings)
    if not admin.get("enable") and not force:
        _log().warning(
            "admin console disabled",
            extra={"event": "admin_disabled", "error_code": "QFSD-9003", "detail": {"hint": "set ADMIN_ENABLE=true"}},
        )
        return 0
    try:
        import uvicorn
    except Exception as exc:
        _log().error("uvicorn unavailable", extra={"event": "admin_dependency_missing", "error": str(exc)[:200]})
        return 3

    bind_host = host or str(admin.get("host") or "127.0.0.1")
    bind_port = int(port or admin.get("port") or 8787)
    if not admin.get("auth_token") and bind_host not in ("127.0.0.1", "::1", "localhost"):
        _log().error(
            "refusing to expose admin console without a token",
            extra={"event": "admin_bind_refused", "error_code": "QFSD-6001"},
        )
        return 3

    app = create_app(settings, admin=admin)
    audit("admin.start", detail={"host": bind_host, "port": bind_port, "readonly": admin.get("readonly", False)})
    _log().info(
        "admin console listening",
        extra={"event": "admin_start", "detail": {"url": f"http://{bind_host}:{bind_port}/", "readonly": admin.get("readonly", False)}},
    )
    uvicorn.run(app, host=bind_host, port=bind_port, log_config=None, access_log=True)
    return 0


__all__ = [
    "ADMIN_CONFIG_PATH",
    "create_app",
    "effective_admin",
    "masked_settings",
    "read_admin_toml",
    "resolve_web_dir",
    "serve",
]
