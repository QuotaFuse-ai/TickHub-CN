"""CT-02/CT-03/CT-04: response envelope, alias equivalence, error codes."""

from __future__ import annotations

import datetime as _dt
import inspect
import re

import pytest

import tickhub_cn.datasources  # noqa: F401
from tickhub_cn.core.errors import EXIT_CODES, TickHubError
from tickhub_cn.datasources import financials, quotes
from tickhub_cn.datasources.base import ALIASES, REGISTRY, call_tool, resolve

MANDATORY_META = {"source", "fetched_at", "cache_hit", "confidence", "trace_id"}
TRACE_RE = re.compile(r"^[0-9a-f]{16}$")


def _assert_envelope(payload) -> None:
    assert isinstance(payload, dict)
    assert payload["ok"] is True
    assert isinstance(payload["data"], (dict, list)), "data must never be a bare scalar"
    meta = payload["meta"]
    assert MANDATORY_META <= set(meta)
    assert isinstance(meta["source"], str) and meta["source"]
    assert isinstance(meta["cache_hit"], bool)
    assert meta["confidence"] in {"A", "B", "C"}
    assert TRACE_RE.match(meta["trace_id"]), meta["trace_id"]
    fetched = _dt.datetime.fromisoformat(meta["fetched_at"])
    assert fetched.tzinfo is not None
    assert fetched.utcoffset() == _dt.timedelta(hours=8)


def test_envelope_for_computed_tool() -> None:
    payload = call_tool("apply_adjust", {"code": "600000", "closes": "10,11", "factors": "1.1,1.05"})
    _assert_envelope(payload)
    assert payload["data"]["adjusted"] == [11.0, 11.55]
    assert payload["meta"]["source"] == "computed"
    assert payload["meta"]["cache_hit"] is False


def test_envelope_for_upstream_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        financials,
        "em_get",
        lambda *a, **k: {"data": {"f57": "600000", "f58": "浦发银行", "f84": 1e9, "f162": 6.5}},
    )
    payload = call_tool("stock_profile", {"code": "600000"})
    _assert_envelope(payload)
    assert payload["data"]["name"] == "浦发银行"
    assert payload["meta"]["source"] == "eastmoney"
    assert payload["meta"]["confidence"] == "B"


def test_list_response_carries_count(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.support import qt_line

    monkeypatch.setattr(quotes, "request", lambda *a, **k: qt_line())
    payload = call_tool("tencent_quote", {"codes": "600000"})
    _assert_envelope(payload)
    assert payload["meta"]["count"] == 1
    assert payload["data"][0]["code"] == "600000"


# ------------------------------------------------------------------- aliases


def test_every_alias_resolves_to_its_target() -> None:
    for alias, target in ALIASES.items():
        assert resolve(alias) is resolve(target)
        assert resolve(alias) is REGISTRY[target]
        assert inspect.signature(resolve(alias).func) == inspect.signature(resolve(target).func)


def test_alias_and_target_return_identical_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        financials,
        "em_get",
        lambda *a, **k: {"data": {"f57": "600000", "f58": "浦发银行", "f84": 1e9}},
    )
    via_target = call_tool("stock_profile", {"code": "600000", "_no_cache": True})
    via_alias = call_tool("stock_info", {"code": "600000", "_no_cache": True})
    for payload in (via_target, via_alias):
        payload["meta"].pop("trace_id")
    assert via_alias == via_target


# --------------------------------------------------------------- error codes


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        ("QFSD-1001", 4),  # source_unavailable
        ("QFSD-1002", 4),
        ("QFSD-3001", 3),  # config_error
        ("QFSD-4001", 1),  # general
        ("QFSD-5001", 1),
        ("QFSD-9001", 2),
        ("QFSD-9002", 1),
        ("QFSD-9003", 4),
        ("QFSD-9004", 6),
        ("QFSD-9005", 1),
        ("QFSD-8001", 8),
        ("QFSD-2001", 5),
        ("QFSD-7001", 1),
        ("QFSD-6001", 1),
    ],
)
def test_tickhub_error_exit_codes(code: str, exit_code: int) -> None:
    assert TickHubError(code).exit_code == exit_code


def test_exit_code_names_match_spec() -> None:
    assert EXIT_CODES == {
        "ok": 0,
        "general": 1,
        "invalid_argument": 2,
        "config_error": 3,
        "source_unavailable": 4,
        "redis_unavailable": 5,
        "auth_failed": 6,
        "batch_partial_failure": 8,
    }


def test_error_payload_shape() -> None:
    err = TickHubError("QFSD-9001", "invalid A-share code: 60000", detail={"input": "60000"})
    body = err.to_dict()
    assert body["code"] == "QFSD-9001"
    assert body["name"] == "invalid_argument"
    assert body["message"] == "invalid A-share code: 60000"
    assert body["detail"] == {"input": "60000"}
    assert body["retryable"] is False
    assert body["action"]


def test_unknown_tool_raises_9002() -> None:
    with pytest.raises(TickHubError) as caught:
        call_tool("no_such_tool", {})
    assert caught.value.code == "QFSD-9002"
    assert caught.value.exit_code == 1


# ------------------------------------------------------------- CLI exit codes


def test_cli_unknown_tool_exit_1(run_cli_json) -> None:
    code, payload, _ = run_cli_json("tool", "call", "no_such_tool")
    assert code == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] == "QFSD-9002"


def test_cli_bad_code_exit_2(run_cli_json) -> None:
    code, payload, _ = run_cli_json("stock", "info", "60000")
    assert code == 2
    assert payload["error"]["code"] == "QFSD-9001"


def test_cli_unsupported_period_exit_2(run_cli_json) -> None:
    code, payload, _ = run_cli_json("kline", "get", "600000", "--period", "hourly")
    assert code == 2
    assert payload["error"]["code"] == "QFSD-9001"


def test_cli_upstream_failure_exit_4(run_cli_json, monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    from tickhub_cn.core import http as http_core

    from tests.support import BoomSession

    monkeypatch.setattr(
        http_core, "_session_for", lambda source: BoomSession(requests.ConnectionError("blocked by firewall"))
    )
    code, payload, _ = run_cli_json("--no-cache", "kline", "get", "600000")
    assert code == 4
    assert payload["error"]["code"] == "QFSD-9003"
    assert payload["error"]["retryable"] is True


def test_cli_no_data_exit_1(run_cli_json, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(financials, "em_get", lambda *a, **k: {"data": {}})
    monkeypatch.setattr(
        financials,
        "_tencent_profile",
        lambda code: (_ for _ in ()).throw(TickHubError("QFSD-9005", f"no profile for {code}")),
    )
    code, payload, _ = run_cli_json("--no-cache", "stock", "info", "600000")
    assert code == 1
    assert payload["error"]["code"] == "QFSD-9005"
