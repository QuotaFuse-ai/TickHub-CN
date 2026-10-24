# Example: one realtime quote, three ways

Every number below comes from a tool result. Nothing is computed by the model.

## 1. CLI

```bash
th-cn tool call tencent_quote --params '{"codes":"600000"}'
```

`--params` takes a JSON object whose keys match the tool parameters
(`tools.md` lists them). A shorter alias exists for the same function, but
`tencent_quote` is the canonical name.

Output (trimmed):

```json
{
  "ok": true,
  "data": [
    {
      "code": "600000",
      "name": "浦发银行",
      "price": 9.53,
      "prev_close": 9.62,
      "change": -0.09,
      "change_pct": -0.94,
      "volume": 755926,
      "amount": 72634.0,
      "turnover_rate": 0.23,
      "pe": 6.2,
      "pb": 0.42,
      "float_market_cap": 3174.05,
      "total_market_cap": 3174.05,
      "limit_up": 10.58,
      "limit_down": 8.66,
      "market": "SH",
      "is_stale": false
    }
  ],
  "meta": {
    "source": "tencent",
    "fetched_at": "2026-10-24T03:37:54+08:00",
    "cache_hit": false,
    "confidence": "A",
    "trace_id": "0ae47c686b3d468f",
    "count": 1
  }
}
```

Units to quote correctly: volume `755926` is **手** (75.6M shares), amount
`72634` is **万元** (726.34M CNY), market caps are **亿元**.

> A `QFSD-2001` warning on stderr means Redis is unavailable and the
> in-process cache is in use. The data is still valid.

## 2. MCP

Tool call over stdio or HTTP:

```json
{
  "name": "tencent_quote",
  "arguments": { "codes": "600000" }
}
```

The response is the same envelope. `codes` accepts one code or a
comma-separated list. For more than a handful of codes use
`tencent_quote_batch`, which sends 20 per upstream request, caps the call at
`max_codes` (default 50) and isolates per-code failures.

## 3. Python

```python
from tickhub_cn import StockDataClient

client = StockDataClient()
row = client.tencent_quote("600000")["data"][0]
print(row["name"], row["price"], row["volume"], "手", row["amount"], "万元")
```

## Reading the result

- `meta.confidence` is `A` (exchange/Tencent grade) — safe to report.
- `is_stale` is `false` during the session; outside trading hours the last
  session's values are returned with `is_stale` set.
- `meta.trace_id` identifies this exact response; include it when reporting a
  bug.
