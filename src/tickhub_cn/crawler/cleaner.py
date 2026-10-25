"""Optional long-text cleaning for crawled announcements and reports (spec §4.11).

The switch lives on the model gateway (``model_gateway.toml`` / ``CRAWL4AI_ENABLE``)
and is off by default, so the default install returns the raw text untouched
with zero model calls. No ``crawl4ai`` package is imported: cleaning posts to an
OpenAI-compatible ``/chat/completions`` endpoint. Numbers are never rewritten
here — cleaning only removes page chrome (menus, ads, footers).
"""

from __future__ import annotations

import html as _html
import re
from typing import Any

from ..core.config import get_settings
from ..logging.setup import get_logger

_SCRIPT_RE = re.compile(r"<(script|style|noscript|nav|footer|header)[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")

MAX_INPUT = 12000


def _log() -> Any:
    return get_logger("crawler")


def strip_html(text: str) -> str:
    """Deterministic local fallback when no cleaning model is configured."""
    out = _SCRIPT_RE.sub(" ", str(text or ""))
    out = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", out, flags=re.I)
    out = _TAG_RE.sub(" ", out)
    out = _html.unescape(out)
    out = "\n".join(_SPACE_RE.sub(" ", line).strip() for line in out.splitlines())
    return _BLANK_RE.sub("\n\n", out).strip()


def cleaning_enabled() -> bool:
    """True when the gateway is on, or when the legacy ``CRAWL4AI_ENABLE`` is set."""
    if get_settings().crawl4ai_enable:
        return True
    from ..model_gateway import enabled

    return enabled()


def clean_text(text: str, *, prompt: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Return ``{raw_content, clean_content, stat:{raw_len, clean_len, reduce_rate}}``."""
    raw = str(text or "")
    raw_len = len(raw)
    cleaned = raw

    settings = get_settings()
    active = cleaning_enabled()
    if active and raw.strip():
        try:
            from ..model_gateway import chat, enabled, get_gateway_config

            if not enabled():
                _log().warning(
                    "text cleaning enabled but model gateway is off",
                    extra={"event": "clean_skip", "error_code": "QFSD-9003"},
                )
            else:
                conf = get_gateway_config()
                payload = chat(
                    [
                        {
                            "role": "system",
                            "content": prompt or conf.prompt or settings.crawl4ai_prompt,
                        },
                        {"role": "user", "content": raw[:MAX_INPUT]},
                    ],
                    purpose="text_clean",
                    model=model,
                )
                content = str(payload.get("content") or "").strip()
                if content:
                    cleaned = content
        except Exception as exc:
            _log().warning(
                "text cleaning failed, using raw content",
                extra={"event": "clean_failed", "error_code": "QFSD-1001", "error": str(exc)[:200]},
            )

    if active and cleaned is raw and "<" in raw and ">" in raw:
        cleaned = strip_html(raw)

    clean_len = len(cleaned)
    reduce_rate = round(1 - clean_len / raw_len, 4) if raw_len else 0.0
    return {
        "raw_content": raw,
        "clean_content": cleaned,
        "stat": {"raw_len": raw_len, "clean_len": clean_len, "reduce_rate": reduce_rate},
    }


__all__ = ["clean_text", "strip_html"]
