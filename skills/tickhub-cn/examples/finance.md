# Example: statements, cross-check and valuation

## 1. Statements

```bash
th-cn finance report 600000 --granularity full --periods 4
```

`--granularity` is `full`, `income`, `balance` or `cashflow`; `--periods`
defaults to 8 and `--report-date YYYY-MM-DD` pins one period.

Output (trimmed):

```json
{
  "ok": true,
  "data": {
    "code": "600000",
    "balance": [
      {
        "assets": 10405355000000.0,
        "liabilities": 9564670000000.0,
        "equity": 840685000000.0,
        "cash": 442593000000.0,
        "report_date": "2026-06-30 00:00:00",
        "security_code": "600000",
        "security_name_abbr": "浦发银行"
      }
    ],
    "income": [{ "report_date": "2026-06-30 00:00:00", "...": "..." }],
    "cashflow": [{ "report_date": "2026-06-30 00:00:00", "...": "..." }]
  },
  "meta": { "source": "eastmoney", "confidence": "B", "count": 4 }
}
```

Amounts are **yuan**. Null fields stay `null` (banks have no inventory, for
example).

## 2. Cross-check the accounting identities

```bash
th-cn tool call finance_crosscheck --params '{"code":"600000"}'
```

The tool checks `assets = liabilities + equity`,
`net_profit = total_profit - income_tax` and
`ending_cash = beginning_cash + net_cash_increase` with a `tolerance`
(default 1 %) and `strict` (default `true`).

Healthy run:

```json
{
  "ok": true,
  "data": {
    "code": "600000",
    "report_date": "2026-06-30 00:00:00",
    "values": { "...": "..." },
    "crosscheck": { "status": "pass", "tolerance": 0.01, "rules": [{ "rule": "balance_sheet", "status": "pass", "diff": 0.0 }] }
  },
  "meta": { "source": "computed", "confidence": "C" }
}
```

Mismatch run — the payload is still delivered for diagnosis, but flagged:

```json
{
  "ok": false,
  "data": { "...": "...", "crosscheck": { "status": "fail", "...": "..." } },
  "meta": { "source": "computed", "error_code": "QFSD-4001", "retryable": false }
}
```

The CLI exits **1**. Do not use the numbers from a `fail` result; show the
failing rule and the `diff`.

`status: "skip"` means the statement does not expose the fields the rule
needs (common for banks and insurers). That is not a pass — say "not checked".

## 3. Valuation

Forward multiples from consensus EPS plus the live quote:

```bash
th-cn tool call full_valuation --params '{"code":"600000"}'
```

```json
{
  "ok": true,
  "data": {
    "code": "600000",
    "name": "浦发银行",
    "price": 9.53,
    "mcap_yi": 3174.05,
    "pe_ttm": 6.2,
    "pb": 0.42,
    "eps_cur": 1.53,
    "eps_next": 1.64,
    "eps_cur_year": 2026,
    "pe_fwd": 6.23,
    "cagr_pct": 7.2,
    "peg": 0.87,
    "target_pe": 30.0,
    "analyst_count": 13,
    "ratings": { "buy": 3, "overweight": 5, "neutral": 0, "underweight": 0, "sell": 0 },
    "consensus_as_of": "2026-10-23"
  },
  "meta": {
    "source": "tencent",
    "confidence": "A",
    "consensus_source": "ths",
    "quote_source": "tencent"
  }
}
```

- `mcap_yi` is **亿元**; `pe_ttm`, `pb` come from the live quote.
- `eps_*`, `pe_fwd`, `peg`, `cagr_pct` and `ratings` come from the consensus
  provider named in `meta.consensus_source` (`ths`, confidence `C`) — always
  label them as consensus estimates and cite `consensus_as_of`.
- `target_pe` is an assumption you can override with `--params
  '{"code":"600000","target_pe":15}'`; `digest_years` is then derived from it.

Historical multiples:

```bash
th-cn tool call baostock_valuation_history --params '{"code":"600000","start":"2026-01-01","end":"2026-10-23"}'
```

`baostock` does not cover BSE codes; for those use `tencent_quote` PE/PB and
say the history is unavailable.
