"""QUOT-401：版本只在 pyproject.toml 写一次，代码从安装元数据读。"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_dunder_version_reads_install_metadata() -> None:
    """源码树未安装时为 0.0.0+source；已安装时必须等于安装元数据。"""
    from importlib.metadata import PackageNotFoundError, version

    from tickhub_cn import __version__

    try:
        installed = version("tickhub-cn")
    except PackageNotFoundError:
        assert __version__ == "0.0.0+source"
    else:
        assert __version__ == installed


def test_no_hardcoded_pyproject_version_in_source() -> None:
    version = _pyproject_version()
    hits = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if re.search(rf"""["']{re.escape(version)}["']""", path.read_text(encoding="utf-8"))
    ]
    assert hits == [], hits


def test_healthz_body_shape() -> None:
    """四个模块统一：{status, service, version}。"""
    from tickhub_cn import __version__

    from tickhub_cn.mcp.server import healthz_body

    assert healthz_body() == {"status": "ok", "service": "tickhub-cn", "version": __version__}
