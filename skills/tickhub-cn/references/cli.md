# `th-cn` CLI reference

The CLI is the fourth usage mode alongside local import, MCP stdio and MCP
HTTP. It is for scripts, cron jobs, operations and manual queries. Full
narrative: `docs/CLI_USAGE.md`.

## Output contract

- Default `--output json`; also `table`, `csv`, `yaml`.
- Structured data goes to **stdout**; logs, progress and warnings go to
  **stderr**. Piping stdout to `jq` always works.
- Windows: the CLI forces UTF-8 on both streams, so redirecting to a file gives
  UTF-8 JSON. If a terminal shows mojibake for Chinese names, that is the
  console's code page, not the payload.

Success envelope:

```json
{"ok": true, "data": {...}, "meta": {"source": "tencent", "fetched_at": "...",
 "cache_hit": false, "confidence": "A", "trace_id": "..."}}
```

Failure:

```json
{"ok": false, "error": {"code": "QFSD-9001", "name": "invalid_argument",
 "message": "invalid A-share code: 60000", "action": "修正参数",
 "detail": null, "retryable": false}}
```

## Global options

| Option | Default | Meaning |
| --- | --- | --- |
| `--version` | — | print the version, exit 0 |
| `--output`, `-o` | `json` | `json`, `table`, `csv`, `yaml` |
| `--fields` | — | comma-separated projection |
| `--no-cache` | off | bypass the cache for this call |
| `--refresh-cache` | off | refetch and overwrite the cache |
| `--timeout` | 15 s | upstream request timeout |
| `--retries` | 2 | upstream retries |
| `--quiet`, `-q` | off | only warnings and errors on stderr |
| `--verbose`, `-v` | off | debug logging on stderr |

Data commands also accept `--output/--fields/--no-cache/--refresh-cache` after
the command name.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success (also `BrokenPipeError`) |
| 1 | general error, `not_supported`, crosscheck failure |
| 2 | invalid argument |
| 3 | configuration error |
| 4 | source unavailable (timeout / empty / upstream error) |
| 5 | Redis unavailable where strictly required |
| 6 | auth required / admin auth failed |
| 7 | rate limited |
| 8 | batch partial failure |
| 130 | Ctrl-C |

## Business queries

```bash
th-cn stock info 600000
th-cn stock info 600000 --fields code,name,pe,pb --output table

th-cn kline get 600000 --period daily --adjust qfq --limit 320
th-cn kline get 600000 --period weekly --adjust hfq --start 2024-01-01 --end 2024-12-31
th-cn kline batch --codes 600000,000001,300750 --period daily --limit 30

th-cn finance report 600000 --granularity full --periods 8
th-cn finance report 600000 --granularity income --fields report_date,revenue,net_profit

th-cn announcement list 600000 --limit 20 --type 减持
th-cn research list 600000 --limit 10
th-cn risk events 600000 --days 90 --limit 100
```

`kline get` flags: `--period daily|weekly|monthly` (aliases `day`/`week`/`month`;
`hourly` is rejected with `QFSD-9001`, exit 2), `--adjust qfq|hfq|none`
(default `qfq`), `--limit` default 320 / max 2000, `--start`, `--end`.

`kline batch` runs 4 workers, returns
`{total, succeeded, failed, codes, failures}`, keeps successful rows, sets
`meta.error_code = "QFSD-8001"` and exits **8** when any code fails.

`finance report` granularity: `full`, `income`, `balance`, `cashflow`.
`--report-date` pins one reporting period.

## Tool access

```bash
th-cn tool list                  # canonical tools by layer
th-cn tool list --layer quotes
th-cn tool list --aliases
th-cn tool call tencent_quote --params '{"codes": ["600000"]}'
```

`--params` must be a JSON object. It is forwarded unchanged, so the optional
arguments (`fields`, `data_as_of`, `_refresh`, `_no_cache`) work here too.

## Diagnostics

```bash
th-cn doctor                     # sources, cache, config, tool count
th-cn source health
th-cn cache stats
th-cn cache refresh --domain kline --code 600000 --tool tencent_kline
th-cn cache flush --prefix qf:prod:cache:kline
th-cn proxy list
th-cn proxy check --timeout-seconds 5
th-cn config check
th-cn config show
th-cn fieldmap validate
th-cn fieldmap preview --tool eastmoney_financial_report --code 600000
```

Run `th-cn doctor` before retrying a source that looks stale. A missing Redis
shows as `QFSD-2001` and is a warning, not a failure.

## Crawler

```bash
th-cn crawler status
th-cn crawler run --task all
th-cn crawler run --task announcements --codes 600000 --days 30
th-cn crawler daemon
```

Tasks: `announcements`, `reports`, `risks`, `northbound`, `all`. `daemon` blocks and runs on
the cron schedule (`OFFLINE_CRAWLER_CRON`, default `0 2 * * *`) with no port. When
the task set includes `northbound`, a second job snapshots it on
`OFFLINE_CRAWLER_NORTHBOUND_CRON` (default `5 15 * * 1-5`), because the northbound
endpoint publishes nothing by the 02:00 tick. `crawler run` with partial failures exits 8.

## Transports and services

```bash
th-cn mcp stdio
th-cn mcp http --host 127.0.0.1 --port 8000
th-cn admin serve --host 127.0.0.1 --port 8787
th-cn start-all --host 127.0.0.1 --port 8000 --crawler auto
```

`mcp stdio` writes **only** MCP frames to stdout; all logs go to stderr.
`start-all` runs the HTTP server on `--port` and supervises a crawler child in a
separate OS process. The child opens no socket. `--crawler auto` follows
`OFFLINE_CRAWLER_ENABLE`; `on` forces it; `off` serves HTTP only.

## Environment

`th-cn` reads `.env` from the working directory (without overriding real
environment variables) plus the process environment. Common keys:
`REDIS_URL`, `PROXY_URL`, `LOG_LEVEL`, `RUN_MODE`, `OFFLINE_CRAWLER_ENABLE`,
`ADMIN_TOKEN`, `MODEL_GATEWAY_*`. Inspect effective values with
`th-cn config show`; secrets are masked.
