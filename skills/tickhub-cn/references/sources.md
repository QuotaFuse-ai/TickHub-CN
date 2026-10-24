# Data sources, confidence and fallbacks

TickHub-CN talks to public A-share data endpoints directly. There is no
intermediate database: every tool result carries `meta.source` telling you
which upstream answered, and `meta.confidence` grading how much the value can
be trusted.

## Confidence grades

| Grade | Meaning | Typical sources |
| --- | --- | --- |
| `A` | Authoritative primary source. Safe for reporting and for numeric claims. | exchanges (`sse`, `szse`, `bse`), `cninfo`, `tencent`, `cctv`, `pbc`, `stats`, `chinabond`, `chinamoney`, exchange futures (`shfe`, `ine`, `czce`, `cffex`, `gfex`), `sge`, `csindex`, `cnindex` |
| `B` | Reputable aggregator. Good for screening; verify against an `A` source before publishing. | `eastmoney`, `sina`, `baostock`, `mootdx` |
| `C` | Secondary, derived, third-party rank or cache fallback. Directional only. | `ths`, `baidu`, `iwencai`, `cache`, `computed`, `wallstreetcn`, `cls` |

Rules of thumb:

- `meta.confidence == "C"` and `meta.cache_hit == true` → say "roughly", or
  re-run the tool with `_refresh=true` to force a live read.
- Quote `meta.source` next to any number in your answer.
- If two sources disagree on the same fact, prefer the higher grade and
  mention the other value and its source.

## Source groups and what they cover

| Source | Covers | Grade | Notes |
| --- | --- | --- | --- |
| `tencent` (`qt.gtimg.cn`) | realtime quotes, daily/weekly/monthly klines, minute bars, ticks, market snapshot | A | Fast and stable; no auth. Volume is in **手** (lots). |
| `sina` | klines with MA, adjust factors, financial reports, research reports, option chain and greeks | B | Wide coverage; occasionally rate-limits. Volume in **shares**. |
| `eastmoney` (`datacenter-web`, `reportapi`) | financial reports, indicators, boards, fund flow, hot ranks, research reports, news, risk events | B | The `push2.eastmoney.com` host is frequently blocked; prefer tools with a backup. |
| `cninfo` | announcements, announcement risk flags, investor-relations Q&A | A | Official disclosure channel. |
| `sse` / `szse` / `bse` | margin trading, dragon-tiger list, announcements, BSE quotes | A | Exchange-published files; volume in **shares**. |
| `ths` | hot rank, EPS forecasts | C | Ranked lists; numbers are estimates. |
| `baostock` | stock basics, ST list, valuation history | B | Historical bulk data; not realtime. |
| `baidu` | klines with MA | C | Convenience copy of exchange data. |
| `pbc` / `stats` / `chinamoney` / `chinabond` | macro series (CPI, GDP, PMI, LPR, money supply, SHIBOR, repo, yield curve) | A | Official releases; monthly/quarterly cadence. |
| `shfe` / `ine` / `czce` / `cffex` / `gfex` / `sge` | futures and gold spot | A | Exchange daily data. |
| `csindex` / `cnindex` | index lists, constituents, weights, valuation | A | Index provider data. |
| `cctv` / `cls` / `wallstreetcn` | news telegraphs and policy broadcasts | A / C | `cctv` is official; `cls` and `wallstreetcn` are media. |
| `computed` | values TickHub-CN derived itself (versions, crosscheck verdicts, sentiment scores) | C | Not an upstream fact; always show the inputs. |

## Fallback tools

Some upstreams are unreliable from mainland cloud hosts. For those, call the
`*_backup` variant or accept the tool's own fallback:

| Primary | Fallback | When to switch |
| --- | --- | --- |
| `tencent_quote` / `tencent_kline` | `baidu_kline_with_ma`, `sina` tools, `tdx_daily_package` | `QFSD-9003` / `QFSD-1001` on Tencent |
| `margin_detail` / `margin_market` (EastMoney) | `margin_trading_backup` (exchange files) | EastMoney margin endpoint blocked |
| `dragon_tiger` | `dragon_tiger_backup` | `QFSD-9003` on EastMoney |
| `stock_fund_flow` | `fund_flow_history_backup` | EastMoney history blocked |
| `realtime_fund_flow` | `fund_flow_backup` | EastMoney realtime blocked |
| `cninfo_announcements` | `announcements_backup` | cninfo unavailable or paged out |
| `stock_profile` | internal Tencent fallback (`extra.fallback_from = "eastmoney"`) | `push2.eastmoney.com` unreachable |
| `sina_financial_report` | `eastmoney_financial_report`, `eastmoney_financial_all` | Sina report layout changed |

BSE quotes have no Tencent coverage: use `bse_quote_backup`, which reads the
exchange-published BSE files.

When a fallback fires, the envelope's `meta.source` names the source that
actually answered and `meta.extra` may hold `fallback_from`. Report the
effective source, not the tool name.

## Units differ per source

Units are **not** unified across the product. Read the tool's own output and
state the unit next to the number:

| Tool family | Volume | Market cap |
| --- | --- | --- |
| `tencent_quote`, `tencent_quote_batch`, `tencent_kline`, `tencent_minute`, `tencent_ticks`, `tencent_market_snapshot`, `index_quote`, `etf_quote` | 手 (lots); ×100 for shares | 亿元 (`float_market_cap` / `total_market_cap`) |
| `bse_quote_backup`, exchange files (`sse` / `szse`) | shares | yuan |
| `full_valuation` | — | 亿元 as `mcap_yi` |
| `stock_profile` | shares | yuan |
| `tencent_quote.amount` | — | 万元 (10k CNY), as published |
| other `amount` fields | — | yuan |

Volumes from other providers are passed through as published; state the unit
you see rather than assuming one. The same applies to `amount`: Tencent quote
publishes 万元, other providers publish yuan.

Percentages are plain numbers (`6.2` means 6.2%), never strings. Missing
values are `null`, never `0` or `""`.

## Known limitations

- **Redis is optional.** Without a reachable Redis, TickHub-CN falls back to
  an in-process cache and reports `QFSD-2001` once. This is expected, not a
  failure; results stay correct but are not shared across processes.
- **`push2.eastmoney.com` is commonly blocked** on cloud egress. Tools that
  depend on it either fall back automatically or return `QFSD-9003`; use the
  documented backup.
- **Proxy pool.** If every proxy fails the product returns `QFSD-5001` and
  falls back to a direct connection. `QFSD-5001` means "proxy pool dead", not
  "source unavailable".
- **Rate limits.** Upstreams throttle bursts. The product returns
  `QFSD-7001` with a retry suggestion; honour it instead of retrying tightly.
- **Point-in-time data** is bounded by what the upstream publishes. Use
  `data_as_of` to filter rows, and say "as of" when citing history.
- **Non-trading days** return the last trading day's data, flagged in
  `meta.data_as_of`.
- **No derived numbers.** Valuation multiples (`pe_fwd`, `peg`, target PE)
  come from provider forecasts, not from TickHub-CN arithmetic; label them as
  consensus estimates.
