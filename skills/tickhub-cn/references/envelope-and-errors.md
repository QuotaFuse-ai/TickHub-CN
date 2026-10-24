# Envelope, arguments and errors

Every tool, CLI data command and batch response uses one envelope. Anything
outside it (log lines, progress, warnings) goes to stderr or to the log sink.

## 1. Success envelope

```json
{
  "ok": true,
  "data": [{"code": "600000", "name": "浦发银行", "price": 10.21}],
  "meta": {
    "source": "tencent",
    "fetched_at": "2026-10-24T09:31:02+08:00",
    "cache_hit": false,
    "confidence": "A",
    "trace_id": "6f1c0a9d2b7e4c51",
    "count": 1
  }
}
```

| Field | Meaning |
| --- | --- |
| `ok` | `true` unless the call failed or a batch was partial |
| `data` | payload: object, list, or `[]` when the source has no record |
| `meta.source` | source that actually served the call; cite this |
| `meta.fetched_at` | ISO-8601 with +08:00 offset |
| `meta.cache_hit` | `true` when served from cache |
| `meta.confidence` | `A` official/exchange, `B` aggregator, `C` derived/scrape |
| `meta.trace_id` | 16-hex correlation id; include it in written reports |
| `meta.data_as_of` | echoed when a point-in-time view was requested |
| `meta.count` | row count where applicable |
| `meta.note` | explanation for an empty or degraded result |
| `meta.fallback_from` | source that failed before the current one answered |
| `meta.error_code` | present on partial/degraded responses |

## 2. Error envelope

```json
{
  "ok": false,
  "error": {
    "code": "QFSD-9001",
    "name": "invalid_argument",
    "message": "unsupported period: hourly",
    "action": "修正参数",
    "detail": null,
    "retryable": false
  }
}
```

Errors raise in Python, become `ok: false` over MCP, and map to a process exit
code in the CLI. `QF-xxxx` input is accepted and normalised to `QFSD-xxxx`.

| Code | Name | Retryable | Meaning / action |
| --- | --- | --- | --- |
| QFSD-1001 | source_timeout | yes | upstream timed out; retry or switch source |
| QFSD-1002 | source_empty | yes | upstream returned no rows |
| QFSD-2001 | redis_unavailable | — | degraded to in-process cache; warning |
| QFSD-3001 | field_map_invalid | — | `field_map.toml` malformed; full payload used |
| QFSD-4001 | financial_crosscheck_failed | — | accounting identity failed; data flagged |
| QFSD-5001 | proxy_pool_dead | — | no healthy proxy; direct connection used |
| QFSD-6001 | admin_auth_failed | — | missing/invalid admin token |
| QFSD-7001 | rate_limited | yes | per-source limiter; back off and retry |
| QFSD-8001 | batch_partial_failure | yes | some items failed; see `failures` |
| QFSD-9001 | invalid_argument | no | bad code/date/parameter; fix the request |
| QFSD-9002 | not_supported | no | capability not offered by this source |
| QFSD-9003 | source_error | yes | upstream error or unexpected body |
| QFSD-9004 | auth_required | no | missing API key/token for the source |
| QFSD-9005 | no_data | no | valid request, no matching record |

CLI exit codes: `1` general (incl. `not_supported`, crosscheck), `2` invalid
argument, `3` config, `4` source unavailable (`1001/1002/9003`), `5` Redis,
`6` auth, `7` rate limited, `8` batch partial, `130` Ctrl-C.

## 3. Optional arguments on every tool

| Argument | Type | Effect |
| --- | --- | --- |
| `fields` | `"a,b"` or list | project `data` to those fields (see precedence below) |
| `data_as_of` | ISO-8601 string | drop rows published after that moment |
| `_refresh` | bool | bypass the cache and refetch |
| `_no_cache` | bool | skip cache read and write for this call |

Projection precedence, highest first:

1. `fields` passed on this call;
2. `[tools.<name>].default_fields` in `field_map.toml`;
3. no projection (full payload).

`hide` in `field_map.toml` removes fields after alias mapping. An invalid field
map never breaks delivery: the full payload is returned and `QFSD-3001` is
logged.

`data_as_of` is a filter, not a snapshot store. It inspects `crawl_time`,
`publish_time`, `NOTICE_DATE` and `DATE` in each row and keeps rows at or before
the cutoff. Rows without any timestamp are kept. It cannot undo a vendor's
retroactive revision of a value — for that, query the archived copy.

## 4. Batch semantics

`run_batch` (used by `tencent_quote_batch`, `kline batch`, and batch helpers):

```json
{
  "ok": false,
  "data": {
    "total": 3,
    "succeeded": 2,
    "failed": 1,
    "codes": {"600000": {"ok": true}, "000001": {"ok": true}},
    "failures": [{"code": "BAD", "error": {"code": "QFSD-9001"}}]
  },
  "meta": {"source": "batch", "confidence": "B",
           "error_code": "QFSD-8001", "partial": true, "retryable": true}
}
```

- Limits: 50 codes for quotes/K-lines, 100 for research batch helpers.
- Exceeding the limit raises `QFSD-9001`, not a partial result.
- Successful items are always retained. Over MCP, inspect `ok` and
  `meta.partial`; in the CLI the process exits 8.
- Use batches instead of per-code loops; concurrency is bounded (4 by default).

## 5. Cache

| Tier | TTL |
| --- | --- |
| `realtime` | 60 s |
| `semi_static` | 24 h |
| `static` | 7 days |
| `health` | 120 s |

Key: `qf:{env}:cache:{domain}:{code}:{tool}:{params_hash}`. Redis is optional;
when it is unreachable the process serves from memory and logs `QFSD-2001`.
`meta.cache_hit` is set on every cached response. `_refresh` refetches and
overwrites; `_no_cache` neither reads nor writes.

## 6. Rate limiting

Each source has its own limiter. A rejection returns `QFSD-7001` with
`retryable: true`; wait, then retry once with `_refresh=true`. Do not loop on a
limiter — repeated calls extend the window.

## 7. Provenance confidence

`A` = exchange, cninfo, PBoC, NBS, ChinaBond, CFETS, CSIndex/CNIndex, futures
exchanges, SGE, CCTV. `B` = Tencent, Sina, Eastmoney, Baostock, mootdx, batch.
`C` = THS, Baidu, iWenCai, CLS, WallStreetCN, cache, computed. Label `C` values
when a report mixes confidence levels; never present `C` as official.
