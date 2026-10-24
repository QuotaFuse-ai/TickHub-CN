"""Structured error codes and exit codes.

Codes follow the specification (QFSD-xxxx). ``QF-xxxx`` aliases are accepted
for compatibility with the earlier draft of the document.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ERROR_CODES: dict[str, dict[str, str]] = {
    "QFSD-1001": {"name": "source_timeout", "message": "数据源超时", "action": "切换备用源"},
    "QFSD-1002": {"name": "source_empty", "message": "数据源空数据", "action": "切源或返回空并标记"},
    "QFSD-2001": {"name": "redis_unavailable", "message": "Redis 不可用", "action": "降级直连并告警"},
    "QFSD-3001": {"name": "field_map_invalid", "message": "字段映射配置错误", "action": "回退全字段并告警"},
    "QFSD-4001": {"name": "financial_crosscheck_failed", "message": "财报勾稽失败", "action": "标记 fail，阻断脏数据"},
    "QFSD-5001": {"name": "proxy_pool_dead", "message": "代理全池失效", "action": "降级直连并告警"},
    "QFSD-6001": {"name": "admin_auth_failed", "message": "Admin 鉴权失败", "action": "拒绝并审计"},
    "QFSD-7001": {"name": "rate_limited", "message": "限流命中", "action": "返回重试建议"},
    "QFSD-8001": {"name": "batch_partial_failure", "message": "批量部分失败", "action": "返回成功/失败列表"},
    "QFSD-9001": {"name": "invalid_argument", "message": "参数错误", "action": "修正参数"},
    "QFSD-9002": {"name": "not_supported", "message": "当前数据源不支持该能力", "action": "查看降级说明"},
    "QFSD-9003": {"name": "source_error", "message": "数据源返回异常", "action": "重试或切源"},
    "QFSD-9004": {"name": "auth_required", "message": "需要鉴权或 API Key", "action": "配置密钥"},
    "QFSD-9005": {"name": "no_data", "message": "未找到数据", "action": "确认代码/日期"},
}

# Earlier draft used QF-xxxx. Keep the numeric part mapping intact.
ALIASES = {code.replace("QFSD-", "QF-"): code for code in ERROR_CODES}

EXIT_CODES: dict[str, int] = {
    "ok": 0,
    "general": 1,
    "invalid_argument": 2,
    "config_error": 3,
    "source_unavailable": 4,
    "redis_unavailable": 5,
    "auth_failed": 6,
    "batch_partial_failure": 8,
}


@dataclass
class TickHubError(Exception):
    code: str
    message: str = ""
    detail: Any = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.code in ALIASES:
            self.code = ALIASES[self.code]
        meta = ERROR_CODES.get(self.code, {})
        if not self.message:
            self.message = meta.get("message", self.code)
        super().__init__(f"{self.code}: {self.message}")

    @property
    def exit_code(self) -> int:
        meta = ERROR_CODES.get(self.code, {})
        name = meta.get("name", "general")
        if name in {"source_timeout", "source_empty", "source_error"}:
            return EXIT_CODES["source_unavailable"]
        if name == "redis_unavailable":
            return EXIT_CODES["redis_unavailable"]
        if name == "auth_required":
            return EXIT_CODES["auth_failed"]
        if name == "batch_partial_failure":
            return EXIT_CODES["batch_partial_failure"]
        if name == "invalid_argument":
            return EXIT_CODES["invalid_argument"]
        if name == "field_map_invalid":
            return EXIT_CODES["config_error"]
        return EXIT_CODES["general"]

    def to_dict(self) -> dict[str, Any]:
        meta = ERROR_CODES.get(self.code, {})
        return {
            "code": self.code,
            "name": meta.get("name"),
            "message": self.message,
            "action": meta.get("action"),
            "detail": self.detail,
            "retryable": self.retryable,
        }


def error_payload(err: TickHubError) -> dict[str, Any]:
    return {"ok": False, "error": err.to_dict()}


def as_error(exc: Exception) -> TickHubError:
    if isinstance(exc, TickHubError):
        return exc
    import requests

    if isinstance(exc, requests.Timeout):
        return TickHubError("QFSD-1001", detail=str(exc), retryable=True)
    return TickHubError("QFSD-9003", message=str(exc) or exc.__class__.__name__, detail=exc.__class__.__name__)
