# QUOT-359：池类 endpoint 限流的长期方案

- 状态：**已采纳**
- 日期：2026-11-05
- 背景单：QUOT-346 / QUOT-359
- 相关：QUOT-349（调用方退避，PR#60）、QUOT-358（peer 受管理起法）

## 问题

池类工具（`limit_up_pool` / `broken_board_pool` / `yesterday_limit_up_pool` 等）是
`ttl="realtime"`，缓存 60 秒，过期即直打东方财富 push2 系列端点。上游对同一
path 有约 25 秒的节奏惩罚，peer 侧用端点门（25s 间隔、3 次/60s、最多等 2s）
提前失败并抛 `QFSD-7001`，设计意图是让工具层回放 stale cache。

实际缺口：

1. stale 副本只存在进程内存 `_LAST_GOOD`，进程重启 / 新实例冷启动后为空；
2. 命中限流时没有可回放的值，`QFSD-7001` 直接冒泡成 `UnexpectedToolError`；
3. 同一个 key 的 N 个并发调用各自 miss，自己把自己的端点门挤爆。

## 选项

### (a) 池类预热：加 crawler 任务 / 缩短轮询 + 延长本地缓存

- 需要新增涨停池/昨日池抓取任务（现有 `DEFAULT_TASKS` / `MARKET_TASKS` 都没有，
  `src/tickhub_cn/crawler/jobs.py:30-32`）。
- 池数据在盘中是分钟级变化的：延长 realtime TTL 会让 LadderStock 看到过期池；
  缩短轮询则把上游请求量从「按需」变成「常驻」，反而更容易触发同一 path 的 25s 门。
- 只能覆盖 crawler 已知的固定日期集合，补历史日期仍然直打上游。

**结论：不采用作为主方案。** crawler 继续负责公告/研报/北向/风险/财报/K线/快照，
不接管池类。

### (b) 持久化 last-good + 单飞 + 显式降级标注（采纳）

1. 每次 realtime 工具成功，payload 双写：进程内存 `_LAST_GOOD` + cache 后端
   `qf:{env}:lastgood:*`（TTL 7 天）。
2. `QFSD-7001` 时回放顺序：进程内存 → redis 归档；回放结果在 `meta` 标注
   `degraded="stale_cache"`、`last_good_source`、`stale_age_s`、`retry_after_s`。
3. `Cache.remember` 对同一 key 单飞：并发调用只产生一次上游请求，其余等待后读缓存。
4. MCP 层保留 `QFSD-*` 码，调用方不会再把限流/参数错误读成「工具坏了」。
5. 显式 `cache flush` 同时清归档，避免坏数据以降级名义复活。

## 为什么选 (b)

- 直接消灭结构性缺口：重启/冷启动不再依赖「本进程恰好成功过一次」。
- 不改变数据新鲜度契约：正常路径仍是 60s 实时缓存；只有上游真限流时才回放旧值。
- 成本低：一次额外 redis 写（仅 realtime 工具），7 天 TTL 自动回收。
- 可观测：调用方能从 `meta` 判断是否降级、旧值多久、何时可重试。

## 代价与残余风险

- 降级值是**旧值**，调用方必须尊重 `degraded`；LadderStock 侧需在展示层标注数据时间。
- **同工具不同参数**（一次补多个历史日期）仍会命中 7001：跨日期回放等于数据错配，
  不做。批量补历史由调用方串行 + `retry_after_s` 退避（QUOT-345）。
- redis 不可用时归档退化为进程内存，重启后失效（与缓存本身同一降级级别）。

## 判断标准（何时回看这个决定）

- 若盘中同一 family 的合法需求稳定超过 3 次/60s，说明需要上游侧配额或换源，
  届时再评估 (a) 的预热/合并请求，而不是放宽端点门。
- 若 LadderStock 无法保证「同一 family 一次 sync 只取一个日期」，优先改调用方，
  不要在 peer 侧做跨日期回放。
