# 更新记录

本仓库只保留这一份文档。每次优化或修复，在顶部追加一节：日期 · 版本 · 改了什么 · 影响范围。不写设计长文，不记录本机环境、内网地址、工单编号、第三方仓库对比等敏感信息；安装与使用见 README。

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
