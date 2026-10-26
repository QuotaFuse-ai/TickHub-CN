# 更新记录

本仓库只保留这一份文档。每次优化或修复，在顶部追加一节：日期 · 版本 · 改了什么 · 影响范围。不写设计长文，不记录本机环境、内网地址、工单编号、第三方仓库对比等敏感信息；安装与使用见 README。

## 2026-10-27 · 财报 / K线 / 收盘行情采集（T-102）

- 新增三个采集任务：`financials`（三张报表 + 勾稽校验，按 `report_date|statement` 去重）、`kline`（自选池日线前复权，按 `trade_date|period|adjust` 去重）、`quote`（自选池收盘快照，按 `code|trade_date` 去重）。三者均已加入 `TASKS` 与归档表，事件分别以 `financial` / `kline` / `quote` 发布到 `fc:stockdata:events`。
- 市场类任务只跑 `OFFLINE_CRAWLER_WATCHLIST`，不复用指数成分兜底；自选池为空时整轮返回 `empty` 并在任务详情里写明原因，不发起任何数据源请求。`OFFLINE_CRAWLER_MAX_CODES` 控制单轮股票数。
- 守护进程为三项任务注册独立 cron（均可由环境变量覆盖）：财报 `0 18 * * 6`、财报季 `0 18 * 1,4,7,8,10 1-5`、K线 `15 15 * * 1-5`、行情 `10 15 * * 1-5`。只在 `--task all` 时挂载，单任务守护进程仍只跑自己的主 cron；状态接口与 `doctor` 一并暴露/校验这些 cron。
- 勾稽校验结果写入事件的 `quality_metrics.balance_check_result`；入库保持 `INSERT OR IGNORE`，重跑同一天不会新增行，也不会让 Stream 增长；Redis 不可用时事件落 `event_outbox` 补发，不影响抓取结果。
- `all` 任务与既有四个任务的语义、CLI 参数保持不变（`--task` 的帮助文本补上了三个新值）。

## 2026-10-27 · 历史回填脚本（T-104）

- 新增 `scripts/backfill_cn.py`：把归档库中已有的 `announcement` / `research_report` / `risk_event` / `northbound_flow` 一次性回放进 `fc:stockdata:events`。事件由 `crawler/store.py` 的同一批构造函数生成，`event_id` 与实时链路一致；仅额外补 `source_info.backfill=true`，`crawl_time` 保留归档原值（缺失时留空，不补当前时间）。
- 归档库以只读 URI 打开，`--dry-run` 只统计、不写 outbox、不连 Redis；`--since` / `--until` 按业务日期过滤，`--batch-size` 控制分页。
- 幂等两层：消费端按 `event_id` 去重；脚本本地记录 `event_backfill_sent`，重跑不再向 Stream 追加。投递失败与实时链路共用 `event_outbox`，脚本运行前后各补发一次，Redis 恢复后只补一次。
- 直接投递成功会清掉此前同 ID 的滞留 outbox 行，避免后续 flush 再追加一份、只涨 `XLEN` 不涨数据。
- 一次性脚本，不挂入守护进程，进程结束即停止；数据面结构与既有抓取行为不变。


## 2026-10-26 · 事件发布旁路（T-101）

- 新增 `crawler/event_publisher.py`：把新入库的公告、研报、风险、北向记录组装为契约 v0.2 事件，写入 Redis Stream `fc:stockdata:events`（`XADD ... MAXLEN ~ 100000`）。`event_id` 由 `provider|data_type|symbol|biz_key` 的 SHA-256 决定，重复投递由消费端按 ID 去重。
- `crawler/store.py` 四个写入方法在事务提交后发布事件，且只发布本次真正新增的行；北向快照与历史回填共用 `(trade_date, channel)` 标识，不会重复发事件。
- 新增本地 `event_outbox`（SQLite）：Redis 不可用时事件落盘并按 1s/5s/25s/2m/10m 退避补发，超过 24 小时只告警不丢弃；每轮抓取结束时以及常驻守护进程每 5 分钟各触发一次补发。发布失败不影响抓取结果。
- 每轮抓取结束发送心跳 `cn-YYYYMMDD-{task}`（`ok|partial|failed`），守护进程空闲时每 5 分钟发送 `cn-YYYYMMDD-idle-HHMM`；同一 `run_id` 跨进程只发一次，重跑同一窗口不会让 Stream 增长。
- 新增配置 `EVENT_PUBLISH_ENABLE`（默认 `false`，关闭时不写库、不建文件）与 `FC_EVENT_REDIS_URL`（默认 `redis://127.0.0.1:6379/1`，与缓存 db 0 隔离）。
- 数据面语义、CLI 参数、归档表结构与既有测试保持不变。

## 2026-10-26 · 移除 docs 目录

- `docs/` 目录整个删除，更新记录移到仓库根目录 `CHANGELOG.md`。
- README、`.dockerignore` 与开发约定中的路径同步更新。
- 代码、CLI 与数据面行为不变。

## 2026-10-26 · 文档收敛（1.0.1）

- 移除 `docs/` 下全部历史文档与决策记录，仓库只保留本文件。
- 代码、CLI 与数据面行为不变，发行版本仍为 1.0.1。

## 2026-10-26 · 1.0.1

- 容器镜像发布流程增加启动冒烟校验（MCP `initialize`）。
- 补镜像拉取与运行说明，发布记录与 PyPI / 镜像实际状态对齐。

## 2026-10-26 · 1.0.0

- 首个公开版本：14 个数据层、149 个 MCP 工具，支持 pip / 容器 / 源码三种安装方式。
