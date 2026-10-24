# Example: multi-layer research pass

Question: *"What does the latest public record say about 600519, and is the
number set internally consistent?"*

The point of this example is provenance discipline: every figure below came
from a tool, and each block names the tool, the source and the `trace_id` so a
reviewer can replay it. Do not merge figures from different calls without
keeping their metadata.

## 1. Frame the company

```bash
th-cn tool call stock_profile --params '{"code":"600519"}'
```

```json
{
  "ok": true,
  "data": {
    "code": "600519",
    "name": "贵州茅台",
    "total_shares": 1250081648,
    "float_shares": 1250081648,
    "total_market_cap": 1668859000000.0,
    "float_market_cap": 1668859000000.0,
    "pe": 20.49,
    "pb": 6.64,
    "turnover_rate": 0.46,
    "change_pct": 1.25,
    "industry": null,
    "region": null
  },
  "meta": {
    "source": "tencent",
    "fetched_at": "2026-10-24T03:45:09+08:00",
    "cache_hit": false,
    "confidence": "A",
    "trace_id": "a1f715b64bf74041",
    "fallback_from": "eastmoney"
  }
}
```

`fallback_from` means the EastMoney profile host was unreachable and the tool
used the Tencent quote instead; `industry` is therefore `null`. Report the gap
instead of filling it in from memory.

## 2. Check the statements before quoting them

```bash
th-cn finance report 600519 --granularity full --periods 1
th-cn tool call finance_crosscheck --params '{"code":"600519"}'
```

```json
{
  "ok": true,
  "data": {
    "code": "600519",
    "report_date": "2026-06-30 00:00:00",
    "values": {
      "assets": 309050784569.31,
      "liabilities": 46954432394.95,
      "equity": 262096352174.36,
      "total_profit": 61438419177.29,
      "income_tax": 15405088610.51,
      "net_cash_increase": 58385486034.9
    },
    "crosscheck": {
      "status": "pass",
      "tolerance": 0.01,
      "rules": [
        {"rule": "balance_sheet", "status": "pass", "diff": 0.0, "relative": 0.0},
        {"rule": "net_profit", "status": "skip", "reason": "missing fields"},
        {"rule": "cash_flow", "status": "skip", "reason": "missing fields"}
      ]
    }
  },
  "meta": {"source": "computed", "confidence": "C", "trace_id": "a1f715b64bf74041"}
}
```

Only `balance_sheet` was actually checked: `46954432394.95 + 262096352174.36
= 309050784569.31`. The two `skip` rules are **not** a pass — say "not
checked", never "consistent". If any rule returned `fail`, the envelope would
be `ok: false` with `QFSD-4001` and the CLI would exit 1; do not cite the
statement as clean.

## 3. Read the primary disclosure

```bash
th-cn announcement list 600519 --limit 3
```

```json
{
  "ok": true,
  "data": {
    "code": "600519",
    "announcements": [
      {
        "title": "贵州茅台2026年半年度报告",
        "publish_time": "2026-08-15 00:00:00",
        "url": "http://static.cninfo.com.cn/finalpage/2026-08-15/1225475868.PDF"
      },
      {
        "title": "贵州茅台关于会计政策变更的公告",
        "publish_time": "2026-08-15 00:00:00",
        "url": "http://static.cninfo.com.cn/finalpage/2026-08-15/1225475864.PDF"
      }
    ],
    "total": 1284,
    "page": 1
  },
  "meta": {"source": "cninfo", "confidence": "A", "count": 3,
           "trace_id": "a1f715b64bf74041"}
}
```

The report date in step 2 (`2026-06-30`) matches the H1 report published
`2026-08-15`. That is the citation for the statement figures; quote the URL,
not the title alone.

## 4. Add sell-side context

```bash
th-cn research list 600519 --limit 3
th-cn tool call full_valuation --params '{"code":"600519"}'
```

```json
{
  "ok": true,
  "data": {
    "code": "600519",
    "price": 1335.0,
    "mcap_yi": 16688.59,
    "pe_ttm": 20.49,
    "eps_cur": 67.16,
    "eps_next": 71.08,
    "eps_cur_year": 2026,
    "pe_fwd": 19.88,
    "cagr_pct": 5.8,
    "peg": 3.41,
    "target_pe": 30.0,
    "analyst_count": 46,
    "ratings": {"buy": 75, "overweight": 5, "neutral": 0,
                "underweight": 0, "sell": 0},
    "consensus_as_of": "2026-10-23"
  },
  "meta": {"source": "tencent", "confidence": "A",
           "consensus_source": "ths", "quote_source": "tencent",
           "trace_id": "a1f715b64bf74041"}
}
```

The consensus EPS is source `ths` (confidence `C` as a vendor scrape); the
quote half is Tencent (`A`). If you mix them in one sentence, label the
consensus part.

Recompute the forward multiple only if you need to show the arithmetic:

```
pe_fwd = price / eps_next = 1335.0 / 71.08 = 18.78
```

That is a derived figure (confidence `C`); state both inputs and the formula.
The tool's own `pe_fwd` is `19.88`, which uses `eps_cur` (the current-year
estimate), so do not present one as the other.

## 5. Assemble the answer

| Claim | Value | Tool | Source / confidence | trace_id |
| --- | --- | --- | --- | --- |
| Price | 1335.0 CNY | `stock_profile` | tencent / A | `a1f715b64bf74041` |
| H1 assets | 309,050,784,569.31 CNY | `finance_crosscheck` | computed from exchange filings / C | `a1f715b64bf74041` |
| Balance check | pass (diff 0.0) | `finance_crosscheck` | computed / C | `a1f715b64bf74041` |
| H1 report | 2026-08-15 PDF | `cninfo_announcements` | cninfo / A | `a1f715b64bf74041` |
| Consensus EPS 2026 | 67.16 | `full_valuation` | ths / C | `a1f715b64bf74041` |
| Analyst count | 46 | `full_valuation` | ths / C | `a1f715b64bf74041` |

Two gaps to state explicitly:

- `stock_profile.industry` is `null` (Tencent fallback has no industry field).
- `net_profit` and `cash_flow` crosschecks were skipped for missing fields.

A real CLI session produces a different `trace_id` per invocation; keep
whichever one is attached to the call you actually cite.
