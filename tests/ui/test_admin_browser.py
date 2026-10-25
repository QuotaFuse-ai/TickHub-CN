"""UI-04: the admin console driven by a real headless browser.

The other UI tests read ``web/admin/index.html`` as text and, at most, run
extracted helpers under Node. Here the page is served by an actual uvicorn
process and rendered by headless Chrome/Edge, so the assertions cover what the
browser builds: the dashboard cards from ``/api/status``, the northbound
panel from the archive table, and the visible 401 path.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

ADMIN_PAGE = Path(__file__).resolve().parents[2] / "web" / "admin" / "index.html"

_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)

_CANDIDATES = (
    "chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "msedge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)


def _browser() -> str:
    for candidate in _CANDIDATES:
        found = candidate if os.path.isabs(candidate) and Path(candidate).exists() else shutil.which(candidate)
        if found:
            return found
    pytest.skip("no headless Chrome/Edge on this machine")


def _render(url: str, profile: Path) -> str:
    """Load ``url`` in headless Chrome and return the DOM after its JS ran."""
    exe = _browser()
    base = [
        exe,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--hide-scrollbars",
        "--dump-dom",
        "--virtual-time-budget=8000",
        f"--user-data-dir={profile}",
        url,
    ]
    for flag in ("--headless=new", "--headless"):
        base[1] = flag
        proc = subprocess.run(
            base, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
        )
        if proc.returncode == 0 and "<html" in (proc.stdout or ""):
            return proc.stdout
    pytest.fail(f"headless browser failed: {proc.stderr[-400:]}")


def _visible(dom: str) -> str:
    """The DOM minus inline scripts: ``--dump-dom`` also echoes the source, and
    template literals there would match strings the browser never rendered."""
    return _SCRIPT_RE.sub("", dom)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def serve_admin() -> Iterator[Any]:
    """Start admin consoles on demand; every one is shut down at teardown."""
    uvicorn = pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")
    from tickhub_cn.admin_api.app import create_app
    from tickhub_cn.core.config import get_settings

    running: list[tuple[Any, threading.Thread]] = []

    def _start(token: str = "") -> str:
        admin = {"auth_token": token, "readonly": False, "web_dir": str(ADMIN_PAGE.parent)}
        app = create_app(get_settings(), admin=admin)
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        running.append((server, thread))
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            pytest.fail("admin console did not start")
        return f"http://127.0.0.1:{port}"

    try:
        yield _start
    finally:
        for server, thread in running:
            server.should_exit = True
        for _server, thread in running:
            thread.join(timeout=10)


def test_the_dashboard_is_built_by_the_browser(serve_admin: Any, tmp_path: Path) -> None:
    from tickhub_cn.datasources import REGISTRY

    url = serve_admin()

    page = _visible(_render(f"{url}/#/dashboard", tmp_path / "profile"))

    assert "<h2>概览</h2>" in page
    assert "工具数" in page
    assert f">{len(REGISTRY)}<" in page  # the count is fetched from /api/status
    # The bar chart and the layer table exist only after the JS renders.
    assert "<svg" in page
    assert "各层工具数" in page
    assert "quotes" in page


def test_the_northbound_panel_shows_archive_rows(serve_admin: Any, tmp_path: Path) -> None:
    from tickhub_cn.crawler.store import get_archive

    get_archive().insert_northbound(
        [{"channel": "hk2sh", "name": "沪股通", "net_inflow": 123456789.0,
          "net_inflow_wan": 123456.0, "status": 1}],
        trade_date="2026-10-23",
        snapshot_at="2026-10-23T15:05:00",
    )
    url = serve_admin()

    page = _visible(_render(f"{url}/#/northbound", tmp_path / "profile"))

    assert "<h2>北向资金</h2>" in page
    assert "2026-10-23" in page
    assert "沪股通" in page
    assert "1.23" in page  # 123,456,789 元 rendered as 亿元 by the page
    assert "暂无快照" not in page


def test_backfilled_rows_do_not_claim_zero_net_inflow(serve_admin: Any, tmp_path: Path) -> None:
    """Datacenter backfill carries turnover only; a null net inflow must stay a dash."""
    from tickhub_cn.crawler.store import get_archive

    get_archive().insert_northbound(
        [{"channel": "hk2sh", "name": "沪股通", "deal_amount_wan": 15625100.0,
          "deal_num": 8382898, "net_inflow": None, "quota_text": "额度充足"},
         {"channel": "hk2sz", "name": "深股通", "deal_amount_wan": 15692726.0,
          "deal_num": 8731057, "net_inflow": None, "quota_text": "额度充足"}],
        trade_date="2026-10-23",
        source="eastmoney_datacenter",
    )
    url = serve_admin()

    page = _visible(_render(f"{url}/#/northbound", tmp_path / "profile"))

    assert "3131.78" in page  # 北向成交额(亿) sums the two channels
    for label in ("北向净流入(亿)", "南向净流入(亿)"):
        card = re.search(re.escape(label) + r'</div><div class="v ">([^<]*)</div>', page)
        assert card and card.group(1) == "—", label


def test_an_empty_archive_explains_how_it_gets_filled(serve_admin: Any, tmp_path: Path) -> None:
    url = serve_admin()

    page = _visible(_render(f"{url}/#/northbound", tmp_path / "profile"))

    assert "暂无快照" in page
    assert "归档表还没有北向数据" in page
    assert "15:05" in page


def test_a_missing_token_is_reported_in_the_page(serve_admin: Any, tmp_path: Path) -> None:
    url = serve_admin(token="t0ken")

    page = _visible(_render(f"{url}/#/dashboard", tmp_path / "profile"))

    assert "加载失败" in page
    assert "401" in page
    assert "各层工具数" not in page
