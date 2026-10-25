# TickHub-CN

A-share (mainland China) market data, packaged as structured tools for AI
agents. TickHub-CN exposes **124 MCP tools** (plus 25 compatibility aliases)
over **MCP stdio** and **MCP Streamable HTTP**, and the same surface over a
scriptable CLI (`th-cn`).

The rule the product is built around: **every number an agent reports must come
from a tool call.** TickHub-CN never asks a model to recall, estimate or infer a
price, a financial line item or a flow figure — it fetches, normalises, caches
and labels it with provenance.

```
th-cn (CLI)  ─┐
              ├─► tool registry (124 tools, 14 layers) ─► upstream sources
MCP stdio ────┤                                          Eastmoney · Tencent ·
MCP HTTP  ────┘                                          CNINFO · Sina · exchanges
```

- Apache-2.0, Python ≥ 3.11, single wheel (`pip install tickhub-cn`).
- No API key required for the core sources. Optional sources (iWencai, Tushare,
  akshare, baostock) switch on with environment variables.
- Degrades instead of failing: Redis optional (falls back to in-process memory),
  per-source health probing, batch calls report partial failures explicitly.

## Install

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"         # or: pip install tickhub-cn
```

Optional feature groups: `.[admin]` (Admin Console),
`.[sources]` (akshare / baostock / pandas), `.[db]` (log DB), `.[all]`.
Optional long-text cleaning needs no extra: it posts to an OpenAI-compatible
`/chat/completions` endpoint and is switched on from the admin console
(`model_gateway.toml`), off by default.
`.[sources-legacy]` installs mootdx on request; it pins `httpx<0.26` and
`tenacity<9`, so it is kept out of the default extra. Only `stock_f10_tips`
imports it, lazily at call time.

## Quick start

```bash
cp .env.example .env            # optional; every value has a default
th-cn doctor                    # environment + source health, run this first
th-cn stock info 600000
th-cn kline get 600000 --period daily
th-cn finance report 600000 --granularity income
th-cn tool list                 # the full MCP catalogue
```

Run everything as one supervised unit:

```bash
th-cn start-all --port 8000
```

`start-all` forks **two OS processes and opens exactly one port**:

| Process | Role | Port |
| --- | --- | --- |
| main | Streamable HTTP MCP server at `http://127.0.0.1:8000/mcp` | 8000 |
| child (spawn) | background timed crawler (pre-warm, not a request gate) | none |

The main process supervises the child: it restarts it on crash, stops
restarting after three crashes in five minutes and raises a severe alert while
MCP keeps serving, and turns `SIGINT`/`SIGTERM` into a graceful child shutdown.

## MCP client configuration

stdio (client spawns the process, stdout carries protocol frames only):

```json
{
  "mcpServers": {
    "tickhub-cn": {
      "command": "th-cn",
      "args": ["mcp", "stdio"]
    }
  }
}
```

Streamable HTTP:

```bash
th-cn mcp http --host 127.0.0.1 --port 8000     # endpoint: /mcp
```

Every tool returns the same envelope:

```json
{
  "data": [ ... ],
  "meta": {
    "source": "tencent",
    "fetched_at": "2026-10-24T10:21:05+08:00",
    "cache_hit": false,
    "confidence": "A",
    "trace_id": "9ecc979e8192435b"
  }
}
```

Optional arguments accepted by every tool: `fields="code,name,price"` (projection),
`data_as_of=<ISO-8601>` (point-in-time view — drops rows published later),
`_refresh=true` (refetch), `_no_cache=true` (skip cache).

> **Browsers cannot call MCP-HTTP directly.** MCP Streamable HTTP is a
> JSON-RPC protocol, not REST. A web UI needs either the MCP SDK or a thin
> gateway; `web/index.html` ships an example gateway that proxies a small
> allow-list of reads through the Python client.

## Data layers

| Layer | Tools | Examples |
| --- | --- | --- |
| quotes | 13 | `tencent_quote`, `tencent_kline`, `tencent_minute`, `tencent_ticks` |
| financials | 13 | `stock_profile`, `eastmoney_financial_report`, `cninfo_financial_notes` |
| market | 23 | `market_overview`, `limit_up_pool`, `dragon_tiger`, `industry_boards` |
| funds | 16 | `stock_fund_flow`, `northbound_flow`, `margin_detail`, `block_trades` |
| macro | 12 | `china_gdp`, `china_cpi`, `china_pmi`, `china_money_supply` |
| announcements | 9 | `cninfo_announcements`, `cls_telegraph`, `cctv_news` |
| index | 10 | `index_kline`, `index_constituents`, `sw_industry_history` |
| futures | 7 | `futures_realtime`, `futures_position_rank`, `sge_spot` |
| event | 7 | `earnings_forecast`, `share_buyback`, `holder_trades` |
| research | 6 | `eastmoney_reports`, `eastmoney_report_pdf`, `ths_eps_forecast` |
| sentiment | 3 | `cninfo_irm`, `sse_e_interaction`, `em_hot_concept` |
| option | 3 | `sina_option_codes`, `sina_option_greeks`, `sina_option_tquote` |
| bond | 1 | `convertible_bonds` |
| valuation | 1 | `full_valuation` |

`th-cn tool list --layer quotes` filters; `th-cn tool call <name> --params
'{"code":"600000"}'` invokes anything from the CLI.

## CLI

Default output is JSON on **stdout**; logs, warnings and alerts go to
**stderr**, so `th-cn ... > data.json` always yields clean data. Global flags:
`--output json|table|csv|yaml`, `--fields`, `--no-cache`, `--refresh-cache`,
`--timeout`, `--retries`, `--quiet`, `--verbose`.

Maintenance commands:

```bash
th-cn doctor                     # env, cache backend, source health
th-cn source health              # per-source probe with latency
th-cn cache stats | refresh | flush
th-cn crawler status | run --task all | daemon
th-cn fieldmap validate | preview --tool tencent_quote
th-cn config check | show --mask-sensitive
th-cn proxy list | check
th-cn mcp stdio | mcp http --port 8000
th-cn admin serve                # optional, off by default, 127.0.0.1:8787
```

Full command reference: [docs/CLI_USAGE.md](docs/CLI_USAGE.md).

## Configuration

Everything is environment-driven (`.env` is loaded if present); secrets are
masked by default. The files below are optional and ship with safe defaults:

| File | Purpose |
| --- | --- |
| `.env` | runtime settings (copy `.env.example`) |
| `field_map.toml` | external field aliases, hidden fields, default projections |
| `proxy_pool.json` | authenticated proxy nodes (requires `PROXY_POOL_ENABLE=true`) |
| `model_gateway.toml` | optional multi-model gateway (off; never used for numbers) |
| `config/admin.toml` | Admin Console settings |

A missing file is not an error — the built-in behaviour applies. Malformed
files are reported as `QFSD-3001` and the safe fallback is used.

### Caching and resilience

Redis is optional. When reachable, TickHub-CN uses tiered TTLs per data layer
(`realtime` → `semi_static` → `static`) under
`qf:{env}:cache:{domain}:{code}:{granularity}:{params_hash}`; when it is not,
the cache degrades to an in-process store and `doctor` reports `degraded` with
`QFSD-2001`. A failed upstream triggers the next source in the fallback chain;
a batch call returns per-item failures instead of discarding successes.

## Errors

Responses carry `ok`, `data`, `meta`, and on failure a structured error with a
stable code, a message and a suggested action. Codes follow `QFSD-xxxx`
(`QF-xxxx` aliases are still accepted):

| Code | Meaning | CLI exit |
| --- | --- | --- |
| QFSD-1001 / 1002 | source timeout / empty | 4 |
| QFSD-2001 | Redis unavailable (degraded) | 5 |
| QFSD-3001 | field map invalid | 3 |
| QFSD-4001 | financial cross-check failed | 1 |
| QFSD-5001 | proxy pool dead (direct fallback) | 1 |
| QFSD-6001 | admin auth failed | 6 |
| QFSD-7001 | rate limited | 1 |
| QFSD-8001 | batch partial failure | 8 |
| QFSD-9001 … 9005 | invalid argument, not supported, source error, auth required, no data | 2 / 1 / 4 / 6 / 1 |

## Where the product never goes

- No fabricated values: a tool that cannot reach a source returns
  `QFSD-9003`/`QFSD-9005`, never a model guess.
- No silent stale data: every envelope carries `fetched_at`, `cache_hit` and a
  `trace_id`; `data_as_of` gives an explicit point-in-time view.
- No hidden degradation: fallbacks are recorded in `meta` (`fallback_from`,
  `source`); Redis loss is logged with `QFSD-2001`.
- No cross-call state: importing `tickhub_cn` has no side effects and starts no
  sockets — servers start only from an explicit entry point.

## Documentation

- [docs/CLI_USAGE.md](docs/CLI_USAGE.md) — every command, flag and exit code
- [docs/MCP_API_CONTRACT.md](docs/MCP_API_CONTRACT.md) — tool contract, envelope, aliases
- [docs/DATA_MODEL.md](docs/DATA_MODEL.md) — entities, fields, units, cache keys
- [docs/TEST_PLAN.md](docs/TEST_PLAN.md) — contract / integration / regression / UI
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — pip, Docker, systemd, reverse proxy
- [docs/PIP_DISTRIBUTION.md](docs/PIP_DISTRIBUTION.md) — packaging and release
- [docs/MULTI_MODEL_INTEGRATION.md](docs/MULTI_MODEL_INTEGRATION.md) — wiring models
- [skills/tickhub-cn/SKILL.md](skills/tickhub-cn/SKILL.md) — agent skill package

## Development

```bash
pip install -e ".[dev]"
pytest                     # unit + contract tests, offline
pytest -m live             # hits real upstreams (network required)
```

## License

Apache-2.0. TickHub-CN is an independent implementation; it is not a fork of,
and does not vendor code from, any other market-data project. Upstream data
remains subject to each provider's terms of use.
