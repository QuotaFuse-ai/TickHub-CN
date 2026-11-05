"""Domain error codes must survive the MCP SDK's exception wrapping."""

from __future__ import annotations

import inspect

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.mcp.server import surfaced


def test_tool_error_keeps_code_and_retry_hint() -> None:
    @surfaced
    def boom(date: str = "") -> dict:
        raise TickHubError("QFSD-7001", "调用过于频繁", detail={"retry_after_s": 25})

    with pytest.raises(ToolError) as err:
        boom()
    text = str(err.value)
    assert "QFSD-7001" in text
    assert "调用过于频繁" in text
    assert "retry_after_s" in text


def test_argument_errors_are_distinguishable_from_pacing() -> None:
    @surfaced
    def bad(code: str) -> dict:
        raise TickHubError("QFSD-9001", "invalid_argument: code required")

    with pytest.raises(ToolError) as err:
        bad(code="")
    assert "QFSD-9001" in str(err.value)
    assert "QFSD-7001" not in str(err.value)


def test_success_and_signature_pass_through() -> None:
    @surfaced
    def echo(code: str = "600000") -> dict:
        return {"data": [], "meta": {"code": code}}

    assert echo(code="000001")["meta"]["code"] == "000001"
    params = inspect.signature(echo).parameters
    assert list(params) == ["code"]
    assert params["code"].default == "600000"
