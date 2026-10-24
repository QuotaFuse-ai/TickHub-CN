---
name: tickhub-cn
description: Use when a task needs real A-share market data through TickHub-CN — quotes, K-lines, financial statements, fund flows, announcements, research, macro, indices, futures, options, bonds, sentiment or valuation. Every number comes from an MCP tool call; never from model memory or estimation. Load this skill only when data must actually be fetched; for general market concepts or strategy discussion with no data requirement, do not load it.
version: 0.1.0
---

# TickHub-CN

TickHub-CN is an MCP server, CLI and Python library that serves A-share data
from exchanges, official disclosure sites and commercial aggregators. It
exposes **121 canonical tools + 25 compatibility aliases = 146 MCP names** across
14 layers. Responses are structured envelopes with provenance metadata.

## When to use

Use this skill when the task requires **actual market data**, for example:

- "What did 600000 close at today?" — quote / K-line
- "Compare the last eight quarters of revenue for 000001" — financials
- "Which sectors had the strongest fund inflow today?" — market / funds
- "Pull the announcements for this stock over the last month" — announcements
- "What is the current 10-year China government bond yield?" — macro
- "Backtest using data as it was known on 2025-03-31" — any tool with `data_as_of`
- "Write a small script that fetches these series" — local import or CLI

Do **not** load it for: explaining a concept, discussing strategy in the
abstract, or any answer that needs no numeric retrieval. Loading it costs a tool
round trip and adds nothing.

## Prerequisites

1. Python 3.11+ and the package installed:

   ```bash
   pip install tickhub-cn              # base: MCP server, CLI, data sources
   pip install "tickhub-cn[admin]"     # optional Admin Console
   ```

2. The `th-cn` executable on `PATH` (`pip` installs a console script). Verify:

   ```bash
   th-cn version
   th-cn tool list | head
   ```

3. One of these transports, configured in the client:

   ```json
   { "mcpServers": { "tickhub-cn": { "command": "th-cn", "args": ["mcp", "stdio"] } } }
   ```

   or a running server at `http://127.0.0.1:8000/mcp` (`th-cn start-all`).

4. Network access to public Chinese data sites. No account, key or captcha is
   required for the default sources. Redis is optional; without it the server
   uses an in-process cache and logs `QFSD-2001` once.

## Core principle

> **All numbers must come from a TickHub-CN tool result. Never infer,
> interpolate, remember or fabricate a price, ratio, flow or holding.**

Consequences you must honour:

- If `data` is empty, report "no data from `<source>`" using `meta.note`. Do not
  estimate a replacement value.
- If you compute a derived figure (spread, growth rate, percentile), show the
  tool-sourced inputs and the formula. The inputs must both be tool results.
- Keep `meta.source` on every cited number, and keep `meta.trace_id` in written
  reports so a reviewer can replay the call.
- `meta.confidence` is `A` (exchange/official), `B` (commercial aggregator) or
  `C` (derived / third-party scrape). Label `C` figures when mixing.
- The optional model gateway only rewrites prose. It never produces numbers.

## Retrieval advice — start with `th-cn doctor`

When data looks stale, empty, or a source seems down, **run diagnostics before
retrying blindly**:

```bash
th-cn doctor
```

`doctor` reports per-source health, cache state, config problems and the
effective tool count. Use its output to decide:

| Doctor / response signal | Action |
| --- | --- |
| source `ok`, response empty | the source has no such record — report the gap |
| source `degraded` / timeout | retry once with `_refresh=true`, then fall back to a backup tool |
| `QFSD-2001` in logs | Redis absent; in-process cache is serving — not a failure |
| `QFSD-9003` / `QFSD-1001` | source error or timeout; try a `*_backup` tool or accept the gap |
| `QFSD-5001` | no healthy proxy; the call used a direct connection |
| cache `hit: true` on stale data | call with `_refresh=true` |

Other retrieval rules:

- **Batch, don't loop.** `tencent_quote_batch` / `tencent_kline` batches and
  `run_batch` tools take up to 50 codes and return per-code failures.
- **Point-in-time first for history.** Pass `data_as_of` when the question is
  about what was known at a past date; otherwise you get the latest revision.
- **Prefer official sources.** Exchange and cninfo data is confidence `A`;
  aggregator data is `B`/`C`. The tool description names the source.
- **Project only what you need.** `fields="code,price,volume"` keeps context
  small on wide responses.
- **Do not retry a 4xx-style argument error.** `QFSD-9001` means the request is
  malformed (bad code, bad date); fix the argument instead.

## Fast path

```bash
# 1. Is the environment healthy?
th-cn doctor

# 2. What tools exist?
th-cn tool list --layer quotes

# 3. Fetch real data
th-cn tool call tencent_quote --params '{"codes": ["600000", "000001"]}'

# 4. One-shot task commands
th-cn kline get 600000 --period daily --adjust qfq --limit 120
th-cn finance report 600000 --granularity income --periods 8
th-cn announcement list 600000 --limit 20
```

Every command prints the same envelope:

```json
{
  "ok": true,
  "data": [{"code": "600000", "name": "浦发银行", "price": 10.21, "...": "..."}],
  "meta": {"source": "tencent", "fetched_at": "2026-10-24T09:31:02+08:00",
           "cache_hit": false, "confidence": "A", "trace_id": "6f1c0a9d2b7e4c51"}
}
```

## Choosing a layer

| Need | Layer | Representative tools |
| --- | --- | --- |
| live price, order book, ticks | `quotes` | `tencent_quote`, `tencent_market_snapshot`, `tencent_ticks` |
| daily/weekly/monthly/min bars | `quotes` | `tencent_kline`, `tencent_minute`, `baidu_kline_with_ma` |
| statements, indicators, valuation history | `financials` | `eastmoney_financial_report`, `eastmoney_finance_indicators`, `baostock_valuation_history` |
| cross-statement consistency | `financials` | `finance_crosscheck` |
| breadth, rankings, limit pools, sectors | `market` | `market_overview`, `limit_up_pool`, `industry_boards` |
| fund flows, margin, ETFs, holders | `funds` | `realtime_fund_flow`, `margin_detail`, `etf_quotes` |
| filings and news text | `announcements` | `cninfo_announcements`, `eastmoney_stock_news` |
| rates, CPI/PPI/GDP, money supply | `macro` | `bond_yield_curve`, `china_cpi`, `china_social_financing` |
| index level, members, weights | `index` | `index_quote`, `index_constituents`, `index_weights` |
| futures and commodities | `futures` | `futures_realtime`, `futures_daily`, `futures_position_rank` |
| earnings dates, unlocks, pledges | `event` | `earnings_forecast`, `equity_pledge`, `ipo_calendar` |
| broker reports and consensus | `research` | `eastmoney_reports`, `ths_eps_forecast`, `iwencai_search` |
| hot lists, IR Q&A | `sentiment` | `em_hot_concept`, `cninfo_irm`, `sse_e_interaction` |
| option chain and greeks | `option` | `sina_option_codes`, `sina_option_greeks` |
| convertible bonds | `bond` | `convertible_bonds` |
| PE/PB percentile and digestion | `valuation` | `full_valuation` |

Full parameter lists: `references/tools.md`.

## Common arguments

Every tool accepts these keyword arguments in addition to its documented ones:

| Argument | Effect |
| --- | --- |
| `fields="a,b"` | project the response to those fields |
| `data_as_of="2025-03-31T15:00:00"` | drop rows published after that moment (point-in-time) |
| `_refresh=true` | bypass the cache and refetch |
| `_no_cache=true` | skip the cache for this call |

## Failure playbook

| Envelope | Meaning | What to do |
| --- | --- | --- |
| `ok: true`, `data: []`, `meta.note` | source has no record | report the gap; do not estimate |
| `ok: false`, `QFSD-9001` | invalid argument | fix code/date/period; do not retry unchanged |
| `ok: false`, `QFSD-9003` / `QFSD-1001` | source error or timeout | try a documented backup tool; otherwise report |
| `ok: false`, `QFSD-9005` | valid request, no matching record | report "no data"; do not estimate |
| `ok: false`, `QFSD-7001` | rate limited (`retryable: true`) | back off, retry once with `_refresh=true` |
| `ok: false`, `QFSD-4001` | financial crosscheck mismatch | surface the mismatch, do not pick one statement |
| `ok: false`, `QFSD-9002` | unknown tool | call `tools/list` and use an existing name |
| `ok: false`, `QFSD-2001` | Redis unavailable | warning only; in-process cache is active |
| `ok: false`, `QFSD-8001`, `meta.partial` | batch had failures | report succeeded and failed codes separately |

Error code table and retry semantics: `references/envelope-and-errors.md`.

## References

- `references/tools.md` — every tool, its layer, parameters and TTL.
- `references/cli.md` — `th-cn` commands, flags, exit codes.
- `references/envelope-and-errors.md` — envelope fields, optional arguments,
  error codes, batch semantics, cache tiers.
- `references/sources.md` — source list, confidence, known limitations,
  fallbacks and units.

## Examples

- `examples/quote.md` — one stock, three ways (CLI, MCP, Python).
- `examples/kline.md` — adjusted daily bars and a simple return calculation.
- `examples/finance.md` — statements plus crosscheck and valuation history.
- `examples/research-workflow.md` — a multi-layer research pass with citations.
- `examples/mcp-config.json` — stdio and HTTP client configuration.
- `examples/python_client.py` — direct library use with error handling.

## Boundaries

- The MCP-HTTP endpoint is **not REST**. Do not point a browser `fetch()` at
  `/mcp`; use the thin gateway in `web/` or an MCP SDK client.
- The HTTP transport has no client authentication. Keep it on loopback or behind
  an authenticating proxy.
- Never put API keys, admin tokens or private data into a prompt.
- TickHub-CN is data infrastructure, not investment advice. Do not present tool
  output as a recommendation.
- If a required dataset is genuinely absent after `doctor` and a backup attempt,
  say so and stop — do not invent coverage.
