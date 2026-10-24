"""CL-01..CL-04 and CL-09: CLI surface, stream separation, formats, exit codes.

Every command runs in-process through Typer's ``CliRunner`` (the ``run_cli``
fixture) so no port is opened and no upstream host is contacted: the Tencent
HTTP call is answered by a fake and failures are raised on purpose.
"""

from __future__ import annotations

import json

import pytest
import requests
import yaml

from tickhub_cn import __version__
from tests.support import kline_payload, qt_line

ENVELOPE_META = {"source", "fetched_at", "cache_hit", "confidence", "trace_id"}

OFFLINE_COMMANDS = [
    ("version",),
    ("doctor",),
    ("tool", "list"),
    ("cache", "stats"),
    ("source", "health"),
    ("proxy", "list"),
    ("crawler", "status"),
    ("fieldmap", "validate"),
    ("config", "check"),
    ("config", "show"),
]


@pytest.fixture
def fake_tencent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answer Tencent quote/kline calls in-process and record the URLs."""
    from tickhub_cn.datasources import quotes

    calls: list[str] = []

    def _fake(source, method, url, *, params=None, **kwargs):
        calls.append(url)
        if "fqkline" in url:
            return kline_payload()
        return qt_line()

    monkeypatch.setattr(quotes, "request", _fake)
    return calls


# --------------------------------------------------------------------------- CL-01


@pytest.mark.parametrize("args", OFFLINE_COMMANDS, ids=lambda a: " ".join(a))
def test_offline_command_returns_an_envelope(run_cli_json, args: tuple[str, ...]) -> None:
    code, payload, _ = run_cli_json(*args)
    assert code == 0
    assert payload["ok"] is True
    assert set(payload["meta"]) >= ENVELOPE_META


def test_version_flag_and_command_agree(run_cli) -> None:
    code, stdout, _ = run_cli("--version")
    assert code == 0
    assert stdout.strip() == f"tickhub-cn {__version__}"

    code, payload, _ = run_cli("version", "-o", "json")
    assert code == 0
    body = json.loads(payload)
    assert body["data"] == {"name": "tickhub-cn", "version": __version__}


def test_tool_list_publishes_119_tools_and_25_aliases(run_cli_json) -> None:
    code, payload, _ = run_cli_json("tool", "list")
    assert code == 0
    names = {item["name"] for item in payload["data"]}
    assert len(payload["data"]) == 144
    assert payload["meta"]["registered"] == 119
    assert payload["meta"]["aliases"] == 25
    assert "get_kline" in names and "tencent_kline" in names
    alias = next(item for item in payload["data"] if item["name"] == "get_kline")
    assert alias["layer"] == "alias"
    assert alias["description"] == "compatibility alias of tencent_kline"

    code, payload, _ = run_cli_json("tool", "list", "--no-aliases")
    assert code == 0
    assert len(payload["data"]) == 119

    code, payload, _ = run_cli_json("tool", "list", "--layer", "quotes")
    assert code == 0
    assert len(payload["data"]) == 13
    assert {item["layer"] for item in payload["data"]} == {"quotes"}

    code, payload, _ = run_cli_json("tool", "list", "--layer", "alias")
    assert code == 0
    assert len(payload["data"]) == 25


def test_cache_stats_reports_ttl_tiers(run_cli_json) -> None:
    code, payload, _ = run_cli_json("cache", "stats")
    assert code == 0
    assert payload["data"]["ttl_seconds"] == {
        "static": 604800,
        "semi_static": 86400,
        "realtime": 60,
        "health": 120,
    }
    assert payload["data"]["stats"]["backend"] in {"memory", "redis"}


def test_config_show_masks_secrets(run_cli_json, monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.core.config import reload_settings

    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "super-secret-token")
    reload_settings()

    code, payload, _ = run_cli_json("config", "show")
    assert code == 0
    assert payload["meta"]["masked"] is True
    assert payload["data"]["admin_auth_token"] == "***"
    assert "super-secret-token" not in json.dumps(payload)


def test_doctor_reports_offline_degradation(run_cli_json) -> None:
    code, payload, _ = run_cli_json("doctor")
    assert code == 0
    assert payload["data"]["status"] in {"ok", "degraded"}
    checks = {item["check"]: item for item in payload["data"]["checks"]}
    assert checks["tools"]["detail"] == "119 tools + 25 aliases"
    assert checks["cache"]["status"] == "warn"  # the fixture forces in-process cache
    assert payload["data"]["advice"].startswith("run `th-cn config check`")


# --------------------------------------------------------------------------- CL-02


def test_stdout_carries_only_the_payload(run_cli, fake_tencent: list[str]) -> None:
    code, stdout, stderr = run_cli("--verbose", "kline", "get", "600000")
    assert code == 0
    payload = json.loads(stdout)
    assert payload["ok"] is True
    assert payload["data"]["klines"][0]["date"] == "2026-10-20"
    for line in stderr.splitlines():
        if line.strip():
            assert isinstance(json.loads(line), dict), line


# --------------------------------------------------------------------------- CL-03


def test_json_is_the_default(run_cli, fake_tencent: list[str]) -> None:
    code, stdout, _ = run_cli("kline", "get", "600000")
    assert code == 0
    assert len(json.loads(stdout)["data"]["klines"]) == 3


def test_table_renders_the_rows(run_cli, fake_tencent: list[str]) -> None:
    code, stdout, _ = run_cli("kline", "get", "600000", "-o", "table")
    assert code == 0
    assert "date" in stdout and "close" in stdout
    assert "2026-10-20" in stdout


def test_csv_renders_the_rows(run_cli, fake_tencent: list[str]) -> None:
    code, stdout, _ = run_cli("kline", "get", "600000", "-o", "csv")
    assert code == 0
    lines = stdout.strip().splitlines()
    assert lines[0] == "date,open,close,high,low,volume,amount"
    assert lines[1].startswith("2026-10-20,10.0,10.2")


def test_yaml_renders_the_envelope(run_cli, fake_tencent: list[str]) -> None:
    code, stdout, _ = run_cli("kline", "get", "600000", "-o", "yaml")
    assert code == 0
    document = yaml.safe_load(stdout)
    assert document["ok"] is True
    assert document["data"]["code"] == "600000"


def test_projection_applies_to_cli_output(run_cli, fake_tencent: list[str]) -> None:
    code, stdout, _ = run_cli("kline", "get", "600000", "--fields", "date,close")
    assert code == 0
    rows = json.loads(stdout)["data"]["klines"]
    assert [set(row) for row in rows] == [{"date", "close"}] * 3


# --------------------------------------------------------------------------- CL-04


def test_exit_0_on_success(run_cli) -> None:
    assert run_cli("version")[0] == 0


def test_exit_2_on_invalid_argument(run_cli) -> None:
    code, stdout, _ = run_cli("stock", "info", "60000")
    assert code == 2
    assert json.loads(stdout)["error"]["code"] == "QFSD-9001"

    assert run_cli("kline", "get", "600000", "--period", "hourly")[0] == 2


def test_exit_3_on_configuration_error(run_cli_json, tmp_path) -> None:
    from tickhub_cn.fieldmap.loader import reload_field_map

    (tmp_path / "field_map.toml").write_text("[tools.tencent_kline\n", encoding="utf-8")
    reload_field_map()

    code, payload, _ = run_cli_json("fieldmap", "validate")
    assert code == 3
    assert payload["data"]["valid"] is False
    assert payload["data"]["error"]


def test_exit_3_when_settings_are_invalid(run_cli_json, monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.core.config import reload_settings

    monkeypatch.setenv("REDIS_URL", "ftp://127.0.0.1:1/0")
    reload_settings()

    code, payload, _ = run_cli_json("config", "check")
    assert code == 3
    assert payload["data"]["status"] == "error"


def test_exit_4_when_the_source_is_unreachable(run_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import quotes

    def boom(*args, **kwargs):
        raise requests.ConnectionError("blocked by policy")

    monkeypatch.setattr(quotes, "request", boom)
    code, stdout, _ = run_cli("kline", "get", "600000")
    assert code == 4
    payload = json.loads(stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "QFSD-9003"
    assert "data" not in payload


def test_exit_8_when_a_batch_item_fails(run_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import quotes

    def flaky(source, method, url, *, params=None, **kwargs):
        if "sz000001" in (params or {}).get("param", ""):
            raise requests.ConnectionError("blocked by policy")
        return kline_payload()

    monkeypatch.setattr(quotes, "request", flaky)
    code, stdout, _ = run_cli("kline", "batch", "--codes", "600000,000001")
    assert code == 8
    payload = json.loads(stdout)
    assert payload["data"]["succeeded"] == 1
    assert payload["data"]["failed"] == 1
    assert payload["meta"]["error_code"] == "QFSD-8001"


def test_exit_1_on_unknown_tool(run_cli) -> None:
    code, stdout, _ = run_cli("tool", "call", "no_such_tool")
    assert code == 1
    assert json.loads(stdout)["error"]["code"] == "QFSD-9002"


def test_exit_130_on_interrupt(run_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.cli import main as cli

    def interrupted() -> dict:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_doctor_payload", interrupted)
    assert run_cli("doctor")[0] == 130


# --------------------------------------------------------------------------- CL-09


def test_fieldmap_preview_projects_a_sample_call(run_cli_json, fake_tencent: list[str]) -> None:
    code, payload, _ = run_cli_json(
        "fieldmap", "preview", "--tool", "tencent_kline", "--code", "600000", "--fields", "date"
    )
    assert code == 0
    preview = payload["data"]
    assert preview["tool"] == "tencent_kline"
    assert preview["params"]["code"] == "600000"
    assert set(preview["payload"]["data"]["klines"][0]) == {"date"}


def test_malformed_field_map_keeps_full_payload(run_cli_json, tmp_path) -> None:
    from tickhub_cn.fieldmap.loader import reload_field_map

    (tmp_path / "field_map.toml").write_text("not toml {{{", encoding="utf-8")
    reload_field_map()

    code, payload, _ = run_cli_json("fieldmap", "validate")
    assert code == 3
    assert payload["data"]["valid"] is False
    assert payload["data"]["tools"] == []
