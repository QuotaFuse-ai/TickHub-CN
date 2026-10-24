"""Environment-backed configuration for TickHub-CN.

All values are read once and cached. Importing this module is side-effect free;
``get_settings()`` is what loads ``.env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

try:  # python-dotenv is a hard dependency, keep a fallback anyway
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


SECRET_HINTS = ("key", "token", "password", "secret", "credential")


@dataclass
class Settings:
    # basic
    log_level: str = "info"
    log_sink: str = "stderr"
    log_file: str = "./logs/tickhub.log"
    redis_url: str = "redis://127.0.0.1:6379/0"
    proxy_url: str = ""
    tdx_path: str = ""
    field_map_path: str = "./field_map.toml"
    run_mode: str = "stdio"
    cache_env: str = "prod"
    http_timeout: float = 15.0
    http_retries: int = 2

    # sources
    iwencai_api_key: str = ""
    iwencai_base_url: str = "https://openapi.iwencai.com"
    tushare_token: str = ""
    enable_akshare: bool = True
    enable_mootdx: bool = True
    enable_iwencai: bool = True
    enable_tushare: bool = False

    # crawl4ai
    crawl4ai_enable: bool = False
    crawl4ai_base_url: str = "https://api.openai.com/v1"
    crawl4ai_api_key: str = ""
    crawl4ai_model: str = "gpt-4o-mini"
    crawl4ai_prompt: str = (
        "提取正文，移除广告、导航、弹窗、无关链接，保留公告和研报全部业务内容，不要篡改原文信息"
    )

    # offline crawler
    offline_crawler_enable: bool = False
    offline_crawler_cron: str = "0 2 * * *"
    offline_crawler_concurrency: int = 2
    offline_crawler_watchlist: str = ""
    offline_crawler_max_codes: int = 50
    offline_crawler_run_on_start: bool = False
    archive_dir: str = "./var/archive"
    archive_db_path: str = "./var/archive/tickhub.db"

    # proxy pool
    proxy_pool_enable: bool = False
    proxy_pool_list: str = ""
    proxy_pool_config_path: str = "./proxy_pool.json"
    proxy_pool_check_timeout: int = 10
    proxy_pool_check_interval: int = 60

    # log db
    log_db_host: str = "127.0.0.1"
    log_db_port: str = ""
    log_db_user: str = ""
    log_db_password: str = ""
    log_db_name: str = "tickhub_log"

    # admin
    admin_enable: bool = False
    admin_host: str = "127.0.0.1"
    admin_port: int = 8787
    admin_auth_token: str = ""
    admin_readonly: bool = False
    admin_cors_origins: str = ""
    admin_audit_sink: str = "file"
    admin_sensitive_mask: bool = True
    admin_web_dir: str = "./web/admin"

    # model gateway
    model_gateway_enable: bool = False
    model_gateway_base_url: str = ""
    model_gateway_api_key: str = ""
    model_gateway_default_model: str = ""
    model_gateway_config_path: str = "./model_gateway.toml"

    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env

        def g(name: str, default: str = "") -> str:
            return e.get(name, default) or default

        return cls(
            log_level=g("LOG_LEVEL", "info").lower(),
            log_sink=g("LOG_SINK", "stderr").lower(),
            log_file=g("LOG_FILE", "./logs/tickhub.log"),
            redis_url=g("REDIS_URL", "redis://127.0.0.1:6379/0"),
            proxy_url=g("PROXY_URL"),
            tdx_path=g("TDX_PATH"),
            field_map_path=g("FIELD_MAP_PATH", "./field_map.toml"),
            run_mode=g("RUN_MODE", "stdio").lower(),
            cache_env=g("CACHE_ENV", "prod"),
            http_timeout=_float(g("HTTP_TIMEOUT"), 15.0),
            http_retries=_int(g("HTTP_RETRIES"), 2),
            iwencai_api_key=g("IWENCAI_API_KEY"),
            iwencai_base_url=g("IWENCAI_BASE_URL", "https://openapi.iwencai.com"),
            tushare_token=g("TUSHARE_TOKEN"),
            enable_akshare=_bool(g("ENABLE_AKSHARE"), True),
            enable_mootdx=_bool(g("ENABLE_MOOTDX"), True),
            enable_iwencai=_bool(g("ENABLE_IWENCAI"), True),
            enable_tushare=_bool(g("ENABLE_TUSHARE"), False),
            crawl4ai_enable=_bool(g("CRAWL4AI_ENABLE"), False),
            crawl4ai_base_url=g("CRAWL4AI_BASE_URL", "https://api.openai.com/v1"),
            crawl4ai_api_key=g("CRAWL4AI_API_KEY"),
            crawl4ai_model=g("CRAWL4AI_MODEL", "gpt-4o-mini"),
            crawl4ai_prompt=g(
                "CRAWL4AI_PROMPT",
                "提取正文，移除广告、导航、弹窗、无关链接，保留公告和研报全部业务内容，不要篡改原文信息",
            ),
            offline_crawler_enable=_bool(g("OFFLINE_CRAWLER_ENABLE"), False),
            offline_crawler_cron=g("OFFLINE_CRAWLER_CRON", "0 2 * * *"),
            offline_crawler_concurrency=_int(g("OFFLINE_CRAWLER_CONCURRENCY"), 2),
            offline_crawler_watchlist=g("OFFLINE_CRAWLER_WATCHLIST"),
            offline_crawler_max_codes=_int(g("OFFLINE_CRAWLER_MAX_CODES"), 50),
            offline_crawler_run_on_start=_bool(g("OFFLINE_CRAWLER_RUN_ON_START"), False),
            archive_dir=g("ARCHIVE_DIR", "./var/archive"),
            archive_db_path=g("ARCHIVE_DB_PATH", "./var/archive/tickhub.db"),
            proxy_pool_enable=_bool(g("PROXY_POOL_ENABLE"), False),
            proxy_pool_list=g("PROXY_POOL_LIST"),
            proxy_pool_config_path=g("PROXY_POOL_CONFIG_PATH", "./proxy_pool.json"),
            proxy_pool_check_timeout=_int(g("PROXY_POOL_CHECK_TIMEOUT"), 10),
            proxy_pool_check_interval=_int(g("PROXY_POOL_CHECK_INTERVAL"), 60),
            log_db_host=g("LOG_DB_HOST", "127.0.0.1"),
            log_db_port=g("LOG_DB_PORT"),
            log_db_user=g("LOG_DB_USER"),
            log_db_password=g("LOG_DB_PASSWORD"),
            log_db_name=g("LOG_DB_NAME", "tickhub_log"),
            admin_enable=_bool(g("ADMIN_ENABLE"), False),
            admin_host=g("ADMIN_HOST", "127.0.0.1"),
            admin_port=_int(g("ADMIN_PORT"), 8787),
            admin_auth_token=g("ADMIN_AUTH_TOKEN"),
            admin_readonly=_bool(g("ADMIN_READONLY"), False),
            admin_cors_origins=g("ADMIN_CORS_ORIGINS"),
            admin_audit_sink=g("ADMIN_AUDIT_SINK", "file"),
            admin_sensitive_mask=_bool(g("ADMIN_SENSITIVE_MASK"), True),
            admin_web_dir=g("ADMIN_WEB_DIR", "./web/admin"),
            model_gateway_enable=_bool(g("MODEL_GATEWAY_ENABLE"), False),
            model_gateway_base_url=g("MODEL_GATEWAY_BASE_URL"),
            model_gateway_api_key=g("MODEL_GATEWAY_API_KEY"),
            model_gateway_default_model=g("MODEL_GATEWAY_DEFAULT_MODEL"),
            model_gateway_config_path=g("MODEL_GATEWAY_CONFIG_PATH", "./model_gateway.toml"),
        )

    def masked(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "extra":
                continue
            value = getattr(self, f.name)
            if any(h in f.name.lower() for h in SECRET_HINTS) and value:
                out[f.name] = "***"
            else:
                out[f.name] = value
        return out

    @property
    def proxy_pool_path(self) -> Path:
        return Path(self.proxy_pool_config_path).expanduser()

    @property
    def field_map_file(self) -> Path:
        return Path(self.field_map_path).expanduser()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    if load_dotenv is not None:
        load_dotenv(override=False)
    return Settings.from_env()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
