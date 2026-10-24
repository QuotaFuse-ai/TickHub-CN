"""CL-10: admin console auth, read-only guard, audit trail (docs/TEST_PLAN.md)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tickhub_cn.admin_api.app import create_app
from tickhub_cn.core.config import get_settings

TOKEN = "s3cret-admin-token"


def _client(**overrides: Any) -> Any:
    from fastapi.testclient import TestClient

    admin = {
        "enable": True,
        "host": "127.0.0.1",
        "port": 8787,
        "auth_token": TOKEN,
        "readonly": False,
        "cors_origins": "",
        "audit_sink": "file",
        "sensitive_mask": True,
    }
    admin.update(overrides)
    return TestClient(create_app(get_settings(), admin=admin))


def _headers(token: str = TOKEN) -> dict[str, str]:
    return {"x-admin-token": token}


def _audit_rows() -> list[dict[str, Any]]:
    path = Path("./logs/admin_audit.jsonl")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_admin_toml_style_cors_list_is_honoured() -> None:
    client = _client(cors_origins=["http://localhost:5173", "http://127.0.0.1:8788"])
    reply = client.get("/api/status", headers={**_headers(), "Origin": "http://localhost:5173"})
    assert reply.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_admin_requires_token_on_every_route() -> None:
    client = _client()
    assert client.get("/healthz").status_code == 200

    for path in ("/api/status", "/api/sources", "/api/config", "/api/audit", "/api/mcp/tools"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers=_headers("wrong")).status_code == 401, path
        assert client.get(path, headers=_headers()).status_code == 200, path

    status = client.get("/api/status", headers=_headers()).json()
    assert status["ok"] is True
    assert status["auth"] == "token"
    assert status["readonly"] is False
    assert status["tools"] == 121
    assert sum(status["layers"].values()) == 121

    bearer = client.get("/api/status", headers={"authorization": f"Bearer {TOKEN}"})
    assert bearer.status_code == 200

    tools = client.get("/api/mcp/tools", headers=_headers()).json()
    assert tools["count"] == 121
    assert len(tools["tools"]) == 121
    quotes = client.get("/api/mcp/tools?layer=quotes", headers=_headers()).json()
    assert len(quotes["tools"]) == 13

    # Loopback clients need no token when none is configured.
    open_client = _client(auth_token="")
    assert open_client.get("/api/status").json()["auth"] == "loopback"


def test_admin_readonly_blocks_every_write() -> None:
    client = _client(readonly=True)
    assert client.get("/api/status", headers=_headers()).json()["readonly"] is True

    assert client.post("/api/cache/flush", headers=_headers(), json={}).status_code == 403
    assert client.post("/api/crawler/run", headers=_headers(), json={"task": "risks"}).status_code == 403
    assert client.post("/api/mcp/call", headers=_headers(), json={"tool": "apply_adjust"}).status_code == 403
    assert client.put("/api/fieldmap", headers=_headers(), json={"content": ""}).status_code == 403
    assert client.post("/api/proxy/reload", headers=_headers()).status_code == 403


def test_admin_writes_are_audited_and_fieldmap_is_validated() -> None:
    client = _client()
    flushed = client.post("/api/cache/flush", headers=_headers(), json={"prefix": "quotes"})
    assert flushed.status_code == 200
    assert flushed.json()["ok"] is True
    assert isinstance(flushed.json()["removed"], int)

    audit = _audit_rows()
    assert audit and audit[-1]["action"] == "cache.flush"
    assert audit[-1]["actor"] == "token"
    assert audit[-1]["detail"]["prefix"] == "quotes"

    bad = client.put("/api/fieldmap", headers=_headers(), json={"content": "[tools\n"})
    assert bad.status_code == 422

    path = Path(get_settings().field_map_path)
    original = path.read_text(encoding="utf-8")
    saved = client.put("/api/fieldmap", headers=_headers(), json={"content": original})
    assert saved.status_code == 200
    assert saved.json()["ok"] is True
    backup = path.with_suffix(path.suffix + ".bak")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == original
    assert path.read_text(encoding="utf-8") == original

    # A populated [tools] table is echoed back, not just accepted.
    custom = '[tools.demo_tool]\ndefault_fields = ["code"]\n'
    populated = client.put("/api/fieldmap", headers=_headers(), json={"content": custom})
    assert populated.status_code == 200
    assert populated.json()["tools"] == ["demo_tool"]
    assert path.read_text(encoding="utf-8") == custom
    from tickhub_cn.fieldmap.loader import reload_field_map

    path.write_text(original, encoding="utf-8")
    reload_field_map()

    actions = [row["action"] for row in _audit_rows()]
    assert "fieldmap.save" in actions


def test_admin_masks_secrets_and_calls_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("TUSHARE_TOKEN", "tk-very-secret")
    from tickhub_cn.core.config import reload_settings

    reload_settings()

    client = _client()
    configured = client.get("/api/config", headers=_headers())
    assert configured.status_code == 200
    assert configured.json()["masked"] is True
    assert configured.json()["config"]["admin_auth_token"] == "***"
    assert configured.json()["config"]["tushare_token"] == "***"
    assert "tk-very-secret" not in configured.text

    called = client.post(
        "/api/mcp/call",
        headers=_headers(),
        json={"tool": "apply_adjust", "params": {"code": "600000", "closes": "10,11", "factors": "2,2"}},
    )
    assert called.status_code == 200
    payload = called.json()["payload"]
    assert payload["ok"] is True
    assert payload["data"] == {"code": "600000", "adjusted": [20.0, 22.0]}

    unknown = client.post("/api/mcp/call", headers=_headers(), json={"tool": "no_such_tool", "params": {}})
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "QFSD-9002"

    actions = [row["action"] for row in _audit_rows()]
    assert "mcp.call" in actions
