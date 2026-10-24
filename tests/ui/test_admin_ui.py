"""UI-03: the admin console is a real page over the REST surface.

The page lives apart from the public read-only gateway (``web/index.html``):
the gateway has no authentication, the console does.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

WEB = Path(__file__).resolve().parents[2] / "web"
ADMIN_PAGE = WEB / "admin" / "index.html"

ENDPOINTS = (
    "/api/status",
    "/api/sources",
    "/api/sources/check",
    "/api/cache/stats",
    "/api/cache/flush",
    "/api/proxy",
    "/api/proxy/check",
    "/api/proxy/reload",
    "/api/fieldmap",
    "/api/fieldmap/preview",
    "/api/logs",
    "/api/mcp/tools",
    "/api/mcp/call",
    "/api/crawler/status",
    "/api/crawler/run",
    "/api/queue",
    "/api/crosscheck",
    "/api/risk",
    "/api/config",
    "/api/audit",
)


def _client(**overrides: Any) -> Any:
    from fastapi.testclient import TestClient

    from tickhub_cn.admin_api.app import create_app
    from tickhub_cn.core.config import get_settings

    admin = {"auth_token": "t0ken", "readonly": False, "web_dir": str(ADMIN_PAGE.parent)}
    admin.update(overrides)
    return TestClient(create_app(get_settings(), admin=admin))


def test_admin_console_is_served_at_root() -> None:
    reply = _client().get("/")
    assert reply.status_code == 200
    assert "TickHub-CN 管理控制台" in reply.text
    assert "数据网关" not in reply.text  # never the unauthenticated gateway page


def test_page_covers_every_admin_route() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    for path in ENDPOINTS:
        assert path in html, path


def test_page_sends_the_admin_token_header() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert "X-Admin-Token" in html
    assert "sessionStorage" in html


def test_page_is_not_an_mcp_client() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    script = "\n".join(re.findall(r"<script>(.*?)</script>", html, flags=re.S))
    assert "fetch(" in script
    assert re.search(r"""["'`]/mcp["'`?]""", script) is None
    assert "jsonrpc" not in script.lower()


def test_render_helpers_escape_upstream_values() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert "function esc(" in html
    assert '\\"&\\": \\"&amp;\\"' in html or '"&": "&amp;"' in html
    table_body = re.search(r"function table\(.*?\n}\n", html, flags=re.S)
    assert table_body
    # Every cell and header of a data table goes through esc().
    assert "esc(row ? row[k]" in table_body.group(0)
    assert "<th>${esc(k)}</th>" in table_body.group(0)
    assert "JSON.stringify(rows, null, 2)" in table_body.group(0)


def test_write_actions_are_guarded_by_readonly() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert "ADMIN_READONLY" in html
    assert "guardReadonly" in html


@pytest.mark.parametrize("path", ["/api/status", "/api/config", "/api/audit"])
def test_served_page_can_authenticate(path: str) -> None:
    client = _client()
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"x-admin-token": "t0ken"}).status_code == 200
