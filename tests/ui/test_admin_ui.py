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
    "/api/crawler/jobs?limit=10",
    "/api/crawler/jobs/${jobId}/${suffix}",
    "/api/archive/northbound?limit=500",
    "/api/queue",
    "/api/crosscheck",
    "/api/risk",
    "/api/config",
    "/api/config/cleaning",
    "/api/config/cleaning/test",
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
    assert "TickHub-CN（A股数据枢纽）管理控制台" in reply.text
    assert "数据网关" not in reply.text  # never the unauthenticated gateway page


def test_page_covers_every_admin_route() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    for path in ENDPOINTS:
        assert path in html, path


def test_page_sends_the_admin_token_header() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert "X-Admin-Token" in html
    assert "sessionStorage" in html


def test_archive_northbound_route_serves_snapshot_and_backfill_columns() -> None:
    from tickhub_cn.crawler.store import get_archive

    get_archive().insert_northbound(
        [{"channel": "hk2sh", "name": "沪股通", "net_inflow": 123456789.0,
          "net_inflow_wan": 123456.0, "status": 1}],
        trade_date="2026-10-23",
        snapshot_at="2026-10-23T15:05:00",
    )
    get_archive().insert_northbound(
        [{"trade_date": "2026-10-22", "channel": "hk2sh", "name": "沪股通",
          "deal_amount_wan": 15625100.0, "deal_num": 8382898, "quota_text": "额度充足"}],
        trade_date="2026-10-22",
        source="eastmoney_datacenter",
    )
    client = _client()
    assert client.get("/api/archive/northbound").status_code == 401

    headers = {"x-admin-token": "t0ken"}
    body = client.get("/api/archive/northbound", headers=headers).json()
    assert body["count"] == 2
    by_date = {row["trade_date"]: row for row in body["rows"]}
    assert (by_date["2026-10-23"]["channel"], by_date["2026-10-23"]["net_inflow"]) == ("hk2sh", 123456789.0)
    backfill = by_date["2026-10-22"]
    assert backfill["deal_amount_wan"] == 15625100.0
    assert backfill["deal_num"] == 8382898
    assert backfill["quota_text"] == "额度充足"
    assert backfill["net_inflow"] is None

    empty = client.get("/api/archive/northbound?trade_date=2026-10-21", headers=headers).json()
    assert empty == {"ok": True, "rows": [], "count": 0}


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
    # The MCP debug call must stay usable in read-only mode; the server refuses
    # only tools that declare themselves writers.
    assert 'data-action="mcp-call" data-allow-readonly="1"' in html
    assert "dataset.allowReadonly" in html


def test_mcp_debug_result_leads_with_envelope_metadata() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert "function metaBanner(" in html
    # Both the success and the error branch put the banner above the raw JSON.
    assert html.count("show(metaBanner(data.payload) + pre(data.payload)") == 1
    assert html.count("show(metaBanner(payload) + pre(payload)") == 1
    assert "meta.data_as_of" in html
    assert "meta.degraded" in html


def test_degraded_labels_cover_every_tag_the_data_layer_emits() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    block = re.search(r"const DEGRADED_LABELS = \{.*?\n\};", html, flags=re.S)
    assert block
    labelled = set(re.findall(r"^\s*([a-z_]+):", block.group(0), flags=re.M))
    emitted: set[str] = set()
    sources = (ADMIN_PAGE.parents[2] / "src" / "tickhub_cn" / "datasources").glob("*.py")
    for source in sources:
        for line in source.read_text(encoding="utf-8").splitlines():
            if "degraded" not in line:
                continue
            emitted.update(re.findall(r'(?:"degraded"\s*:\s*|\["degraded"\]\s*=\s*|degraded=)"([a-z_]+)"', line))
            emitted.update(re.findall(r'\bor\s+"([a-z_]+)"', line))
    assert emitted
    assert not (emitted - labelled), emitted - labelled


def test_meta_banner_renders_degradation_before_the_payload(tmp_path: Path) -> None:
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    script = _script(ADMIN_PAGE.read_text(encoding="utf-8"))
    esc = re.search(r"function esc\(value\).*?\n\}\n", script, flags=re.S)
    banner = re.search(r"const DEGRADED_LABELS = \{.*?\nfunction metaBanner\(.*?\n\}\n", script, flags=re.S)
    assert esc and banner
    probe = esc.group(0) + banner.group(0) + r'''
const degraded = metaBanner({ok: true, meta: {
  source: "eastmoney", data_as_of: "2026-10-23", cache_hit: false, count: 5123,
  degraded: "eod_datacenter", note: "收盘后 <快照>",
}});
const clean = metaBanner({ok: true, meta: {source: "exchange", data_as_of: "2026-10-23", cache_hit: true, count: 0}});
process.stdout.write(JSON.stringify({
  degraded, clean,
  failed: metaBanner({ok: false, error: {code: "QFSD-9003"}}),
  bare: metaBanner({ok: true, meta: {}}),
  unknown: metaBanner({ok: true, meta: {degraded: "brand_new_tag"}}),
}));
'''
    source = tmp_path / "meta_banner.js"
    source.write_text(probe, encoding="utf-8")
    result = subprocess.run(
        [node, str(source)], capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    # No envelope metadata: nothing is invented above the payload.
    assert out["failed"] == ""
    assert out["bare"] == ""
    # Degradation leads, followed by the point-in-time facts.
    assert out["degraded"].startswith('<div class="call-meta degraded">')
    assert out["degraded"].index("降级") < out["degraded"].index("数据时点")
    for needle in ("eod_datacenter", "日频数据中心口径，非实时", "2026-10-23", "eastmoney", "未命中", "5123"):
        assert needle in out["degraded"], needle
    # Upstream text reaches the page escaped, not as markup.
    assert "&lt;快照&gt;" in out["degraded"]
    assert "<快照>" not in out["degraded"]
    # An undegraded envelope still shows when the data is from.
    assert '<div class="call-meta">' in out["clean"]
    assert "降级" not in out["clean"]
    assert "命中" in out["clean"]
    assert "<dd>0</dd>" in out["clean"]
    # A tag the page has not seen yet still warns instead of disappearing.
    assert "brand_new_tag" in out["unknown"]
    assert "降级" in out["unknown"]


def test_archive_table_formats_timestamps(tmp_path: Path) -> None:
    import json
    import shutil
    import subprocess

    html = ADMIN_PAGE.read_text(encoding="utf-8")
    # Both the live job panel and the archive table go through the same helper.
    assert "开始: fmtStamp(job.started_at)" in html
    assert '["提交时间", fmtStamp(job.created_at)]' in html

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = _script(html)
    stamp = re.search(r"function fmtStamp\(value\).*?\n\}\n", script, flags=re.S)
    assert stamp
    probe = stamp.group(0) + r'''
process.stdout.write(JSON.stringify([
  fmtStamp("2026-10-25T16:23:36+08:00"),
  fmtStamp("2026-10-25T16:23:36"),
  fmtStamp(null),
  fmtStamp(""),
  fmtStamp("2026-10-25"),
]));
'''
    source = tmp_path / "fmt_stamp.js"
    source.write_text(probe, encoding="utf-8")
    result = subprocess.run(
        [node, str(source)], capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "2026-10-25 16:23",
        "2026-10-25 16:23",
        "—",
        "—",
        "2026-10-25",
    ]


def test_page_renders_charts_as_inline_svg() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert "function svgBars(" in html
    assert "function svgStack(" in html
    for caller in ("svgBars(Object.entries(status.layers", "svgStack([", "svgBars(latency", "svgBars(throughput"):
        assert caller in html, caller
    # Zero CDN (DEC-017): no external script/style/image URL anywhere.
    assert "https://" not in html
    assert re.search(r"<(script|link|img)\b[^>]*\b(src|href)=", html) is None


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


def test_page_sorts_client_tables_and_leaves_server_order_alone() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert "function sortTable(" in html
    assert "function compareCells(" in html
    assert "data-sortable" in html
    assert 'aria-sort="none"' in html
    # Sortable headers work from the keyboard too.
    assert 'event.key !== "Enter" && event.key !== " "' in html
    # Opting out is explicit: the ordering owner is the server for feeds it sorts.
    assert "opts.sort !== false" in html
    assert html.count("sort: false") >= 4
    assert html.count("顺序由服务端决定") >= 4


def test_sort_helpers_order_numbers_text_and_blanks(tmp_path: Path) -> None:
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    script = _script(ADMIN_PAGE.read_text(encoding="utf-8"))
    match = re.search(r"/\* sort-core \*/.*?/\* sort-core-end \*/", script, flags=re.S)
    assert match
    probe = match.group(0) + r'''
const sorted = ["10", "9", "apple", "Apple", "第10组", "第2组"].sort(compareCells);
const out = {
  numbers: compareCells("9", "10") < 0,
  text: compareCells("alpha", "beta") < 0,
  zhNumeric: sorted.indexOf("第2组") < sorted.indexOf("第10组"),
  caseInsensitive: compareCells("apple", "Apple") === 0,
  blankDash: isBlank("—"),
  blankEmpty: isBlank(""),
  zeroIsReal: !isBlank("0"),
  collapsed: cellText({ textContent: "  a \n b  " }),
};
process.stdout.write(JSON.stringify(out));
'''
    source = tmp_path / "sort_core.js"
    source.write_text(probe, encoding="utf-8")
    result = subprocess.run([node, str(source)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "numbers": True,
        "text": True,
        "zhNumeric": True,
        "caseInsensitive": True,
        "blankDash": True,
        "blankEmpty": True,
        "zeroIsReal": True,
        "collapsed": "a b",
    }


def test_page_searches_across_routes_without_touching_the_server() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert 'id="search-results"' in html
    assert "function scanRoutes(" in html
    assert "state.keepSearch = true" in html
    assert 'data-cross-route' in html
    # Enter in the search box scans the routes the page has not rendered.
    assert 'if (event.key === "Enter") { event.preventDefault(); scanRoutes(); }' in html
    # The scan renders off-screen; it must not write to the page or the server.
    body = re.search(r"async function scanRoutes\(\).*?\n}\n", html, flags=re.S)
    assert body
    assert "fetch(" not in body.group(0)
    assert "post(" not in body.group(0)
    assert 'if (route === currentRoute()) continue;' in body.group(0)


def test_page_exports_the_current_view_as_xlsx_without_dependencies() -> None:
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    assert "function exportViewXlsx(" in html
    for name in ("crc32", "zipStore", "sheetXml", "xlsxBytes", "colName", "xmlText", "headerText", "spreadsheetCell"):
        assert f"function {name}(" in html, name
    assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in html
    assert "[Content_Types].xml" in html
    # store-only ZIP: local header, central directory, end of central directory
    for signature in ("0x04034b50", "0x02014b50", "0x06054b50"):
        assert signature in html, signature
    # The exporter honours the active search and drops the sort arrow from headers.
    assert "if (row.hidden) return;" in html
    assert "headerText(th)" in html


def test_generated_xlsx_is_a_valid_zip_workbook(tmp_path: Path) -> None:
    import json
    import shutil
    import subprocess
    import zipfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    script = _script(ADMIN_PAGE.read_text(encoding="utf-8"))
    match = re.search(r"/\* xlsx-core.*?\*/\n.*?/\* xlsx-core-end \*/", script, flags=re.S)
    assert match
    book = tmp_path / "book.xlsx"
    probe = match.group(0) + f'''
const fs = require("fs");
const bytes = xlsxBytes([{{ name: "结果", rows: [["编号", "名称"], [42, "报价 <ok> & \\"x\\""]] }}]);
fs.writeFileSync({json.dumps(str(book))}, Buffer.from(bytes));
'''
    source = tmp_path / "xlsx_core.js"
    source.write_text(probe, encoding="utf-8")
    result = subprocess.run([node, str(source)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr

    with zipfile.ZipFile(book) as zf:
        assert zf.testzip() is None  # CRC-32 of every member matches
        assert {
            "[Content_Types].xml",
            "_rels/.rels",
            "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels",
            "xl/worksheets/sheet1.xml",
        } <= set(zf.namelist())
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        workbook = zf.read("xl/workbook.xml").decode("utf-8")
    assert "<c r=\"A1\" t=\"inlineStr\"><is><t xml:space=\"preserve\">编号</t></is></c>" in sheet
    assert "<c r=\"A2\"><v>42</v></c>" in sheet
    assert "报价 &lt;ok&gt; &amp; &quot;x&quot;" in sheet
    assert 'name="结果"' in workbook


def test_xlsx_cells_stay_numeric_but_identifier_codes_stay_text(tmp_path: Path) -> None:
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    script = _script(ADMIN_PAGE.read_text(encoding="utf-8"))
    match = re.search(r"function spreadsheetCell\(.*?\n\}\n", script, flags=re.S)
    assert match
    probe = match.group(0) + r'''
const out = ["107", "10.5", "-3", "000001", "007", "600000", "1e6", "—", "", "true", " 42 "]
  .map((value) => spreadsheetCell(value));
process.stdout.write(JSON.stringify(out));
'''
    source = tmp_path / "spreadsheet_cell.js"
    source.write_text(probe, encoding="utf-8")
    result = subprocess.run([node, str(source)], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        107, 10.5, -3, "000001", "007", 600000, "1e6", "—", "", "true", 42,
    ]


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

