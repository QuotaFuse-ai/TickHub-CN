# Example: adjusted daily bars and a return

## Fetch

```bash
th-cn kline get 600000 --period daily --adjust qfq --limit 5
```

- `--period` accepts `daily`, `weekly`, `monthly`. Anything else exits with
  code 2 (`QFSD-9001`).
- `--adjust` accepts `qfq`, `hfq`, `none`. Default is `qfq`.
- `--limit` defaults to 320 and is clamped to 2000.
- `--start` / `--end` filter by `YYYY-MM-DD` after the upstream call.

Same data over MCP:

```json
{ "name": "tencent_kline", "arguments": { "code": "600000", "period": "daily", "adjust": "qfq", "limit": 5 } }
```

Output (trimmed):

```json
{
  "ok": true,
  "data": {
    "code": "600000",
    "period": "daily",
    "adjust": "qfq",
    "klines": [
      { "date": "2026-10-19", "open": 9.72, "close": 9.78, "high": 9.84, "low": 9.65, "volume": 540747.0, "amount": null },
      { "date": "2026-10-20", "open": 9.79, "close": 9.76, "high": 9.86, "low": 9.68, "volume": 605133.0, "amount": null },
      { "date": "2026-10-21", "open": 9.72, "close": 9.66, "high": 9.76, "low": 9.61, "volume": 462562.0, "amount": null },
      { "date": "2026-10-22", "open": 9.66, "close": 9.62, "high": 9.73, "low": 9.55, "volume": 625299.0, "amount": null },
      { "date": "2026-10-23", "open": 9.69, "close": 9.53, "high": 9.73, "low": 9.52, "volume": 755926.0, "amount": null }
    ]
  },
  "meta": { "source": "tencent", "confidence": "A", "cache_hit": false, "count": 5 }
}
```

`amount` is `null` when the upstream omits it. Do not print `0`.

## Derived numbers (allowed, but show your work)

A return is arithmetic on two returned closes. State the inputs and label the
result as derived; never estimate a bar that the tool did not return.

```
close 2026-10-23 = 9.53
close 2026-10-22 = 9.62
return = (9.53 - 9.62) / 9.62 = -0.9356%  (derived from tencent_kline)
```

## Batch with failure isolation

```bash
th-cn kline batch --codes 600000,000001 --period daily --limit 20
```

If one code fails, the payload is `ok: false` with
`meta.error_code = "QFSD-8001"`, counts plus per-code results and failures.
The CLI exits **8** (batch partial failure) while still printing both sides —
use them, do not retry the whole batch blindly.

```json
{
  "ok": false,
  "data": {
    "total": 2,
    "succeeded": 1,
    "failed": 1,
    "codes": { "600000": { "ok": true, "data": { "...": "..." } } },
    "failures": [{ "code": "999999", "error": { "code": "QFSD-1001", "message": "..." } }]
  },
  "meta": { "source": "batch", "error_code": "QFSD-8001", "partial": true, "retryable": true }
}
```

## Adjustment choices

| `--adjust` | Use it for |
| --- | --- |
| `qfq` | Return series and chart continuity (default). |
| `hfq` | Absolute historical price levels. |
| `none` | Raw exchange prices; pair with `sina_adjust_factor` if you need to adjust yourself. |
