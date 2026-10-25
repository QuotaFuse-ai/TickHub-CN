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


def _script(html: str) -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", html, flags=re.S))


def test_page_filters_tables_from_a_global_search_box() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert 'id="search"' in html
    assert 'id="hits"' in html
    assert "function applySearch(" in html
    # Rows are filtered after every view swap, not only on keystrokes.
    assert 'new MutationObserver(applySearch).observe($("#view")' in html
    assert "row.hidden = !hit" in html


def test_page_binds_keyboard_shortcuts_behind_a_typing_guard() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert 'id="shortcuts"' in html
    assert 'id="shortcut-list"' in html
    assert "function isTyping(" in html
    assert 'event.key === "/"' in html
    assert 'event.key === "?"' in html
    assert 'key === "k"' in html
    assert "event.altKey" in html
    # Typing in a field must never trigger the single-key shortcuts.
    assert "if (isTyping(event) || mod) return;" in html


def test_page_exports_the_current_view_as_csv() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert "function csvCell(" in html
    assert "function exportView(" in html
    assert "new Blob(" in html
    assert "text/csv;charset=utf-8" in html
    assert "link.download = name" in html
    # Hidden rows (filtered out by search) stay out of the export.
    assert "if (row.hidden) return;" in html


def test_csv_cells_escape_quotes_commas_and_newlines(tmp_path: Path) -> None:
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    script = _script(ADMIN_PAGE.read_text(encoding="utf-8"))
    match = re.search(r"function csvCell\(.*?\n\}\n", script, flags=re.S)
    assert match
    probe = match.group(0) + r'''
const rows = [csvCell("a,b"), csvCell('say "hi"'), csvCell("x\ny"), csvCell(3), csvCell(null)];
process.stdout.write(JSON.stringify(rows));
'''
    source = tmp_path / "csv_cell.js"
    source.write_text(probe, encoding="utf-8")
    result = subprocess.run([node, str(source)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ['"a,b"', '"say ""hi"""', '"x\ny"', "3", ""]


def test_page_offers_a_manual_three_state_theme() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert 'id="theme"' in html
    assert 'document.documentElement.dataset.theme' in html
    assert 'localStorage.setItem("tickhub_admin_theme"' in html
    assert 'const THEMES = ["auto", "light", "dark"];' in html
    assert ':root[data-theme="dark"]' in html
    # Without JS the page still follows the system preference.
    assert "@media (prefers-color-scheme: dark)" in html


def test_page_glossary_matches_the_error_registry() -> None:
    from tickhub_cn.core.errors import ERROR_CODES

    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert set(re.findall(r"QFSD-\d{4}", html)) == set(ERROR_CODES)
    assert "function codeNotes(" in html
    assert "function codeHelpHtml(" in html
    # Errors shown through the toast carry the glossary too.
    assert "box.textContent = isError ? explain(message) : message;" in html
    assert 'id="code-list"' in html


@pytest.mark.parametrize("path", ["/api/status", "/api/config", "/api/audit"])
def test_served_page_can_authenticate(path: str) -> None:
    client = _client()
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"x-admin-token": "t0ken"}).status_code == 200


def test_health_crosses_the_process_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.admin_api import app as admin_app
    from tickhub_cn.core import http as http_mod

    state = tmp_path / "source_health.json"
    registry = http_mod.HealthRegistry(state)
    # A source name this process never touches, so the local registry cannot
    # shadow the file entry in the merged snapshot.
    registry.record("unit_probe", True, 12.5)
    registry.flush()

    assert state.exists()
    monkeypatch.setattr(admin_app, "read_health_state", lambda *a, **k: http_mod.read_health_state(state))
    snapshot = admin_app._health_snapshot()
    assert snapshot["unit_probe"]["successes"] == 1
    assert snapshot["unit_probe"]["ok"] is True


def test_freshest_health_observation_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.admin_api import app as admin_app

    class _NoCache:
        def read_health(self) -> dict:
            return {}

    old = {"ok": False, "successes": 0, "last_checked_at": "2026-10-25T03:00:00+0800"}
    new = {"ok": True, "successes": 2, "last_checked_at": "2026-10-25T03:10:00+0800"}
    monkeypatch.setattr(admin_app, "read_health_state", lambda *a, **k: {"dual": old})
    monkeypatch.setattr(admin_app, "get_cache", lambda: _NoCache())
    monkeypatch.setattr(admin_app.HEALTH, "snapshot", lambda: {"dual": new})
    assert admin_app._health_snapshot()["dual"] == new


def test_stale_health_state_is_ignored(tmp_path: Path) -> None:
    import json

    from tickhub_cn.core.http import read_health_state

    state = tmp_path / "old.json"
    state.write_text(
        json.dumps({"updated_at": "2020-01-01T00:00:00", "sources": {"tencent": {"ok": True}}}),
        encoding="utf-8",
    )
    assert read_health_state(state) == {}


def test_logs_fall_back_to_the_json_lines_mirror(tmp_path: Path) -> None:
    import json

    from tickhub_cn.admin_api.app import _read_logs
    from tickhub_cn.core.config import Settings

    log_file = tmp_path / "tickhub.log"
    entries = [
        {"ts": "2026-10-25T10:00:00", "level": "info", "logger": "tickhub.http", "message": "quote ok"},
        {"ts": "2026-10-25T10:00:01", "level": "error", "logger": "tickhub.http", "message": "quote failed"},
    ]
    log_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    settings = Settings(log_file=str(log_file), log_sink="stderr")
    rows, origin = _read_logs(settings, limit=10, level="", q="")
    assert origin == "jsonl"
    assert [r["message"] for r in rows] == ["quote failed", "quote ok"]

    errors, _ = _read_logs(settings, limit=10, level="error", q="")
    assert [r["level"] for r in errors] == ["error"]

    hits, _ = _read_logs(settings, limit=10, level="", q="ok")
    assert [r["message"] for r in hits] == ["quote ok"]


def test_console_page_is_shipped_in_the_wheel() -> None:
    assert 'web/admin/index.html' in (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")


def test_web_dir_resolves_from_an_installed_share_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tickhub_cn.admin_api import app as admin_app

    installed = tmp_path / "share" / "tickhub-cn" / "web" / "admin"
    installed.mkdir(parents=True)
    (installed / "index.html").write_text("<html>installed</html>", encoding="utf-8")

    monkeypatch.setattr(admin_app, "sys", type("S", (), {"prefix": str(tmp_path)}))
    monkeypatch.setattr(admin_app, "__file__", str(tmp_path / "site" / "tickhub_cn" / "admin_api" / "app.py"))

    assert admin_app.resolve_web_dir(tmp_path / "missing") == installed

