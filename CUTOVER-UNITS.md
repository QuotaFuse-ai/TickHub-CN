# A 股事件单位切换与存量处置（QUOT-249）

范围：TickHub-CN / TickHub-US 两个仓库的事件字段、单位与 `biz_key` 代次。
不改 FinanceCoffer 代码与公式，不改 DWD 表结构，`0013`（新增 `pb_mrq` /
`amount_net_profit_deducted` 两列）属 QUOT-251，本单不含。
逐字段单位见 [`UNIT-AUDIT.md`](UNIT-AUDIT.md)。

## 1. 结论先写死

**切换方式 = 停采 → 部署带 `|g2` 的版本 → 排空总线 → 有界重灌（版本追加，不是 UPDATE）→ 量级校验。**

三条硬约束决定了只能这么切：

1. `event_id = sha256(provider|data_type|symbol|biz_key)`，**不含内容**。
2. DWD 是 append-only：`dwd.stock_quote_snapshot` 上有 `BEFORE UPDATE` 触发器
   `dwd.forbid_history_update()`（migration 0004），任何业务列 UPDATE 都被拒。
3. `UNIQUE (event_id)`（`uq_quote_event`）：同一业务键重发会被当成重放丢掉，
   修好的金额根本落不了库。

⇒ 「有界 UPDATE 修历史行」这条路在本仓库不存在（第 4 步有 QA 实录的拒绝信息）；
⇒ 修正要生效，事件必须换 `biz_key`。所以 quote / financial 的键尾带字段代次标记
`|g2`，`g2` = 市值为元 + `pb_mrq` + `amount_net_profit_deducted`。换键之后
`QuoteSnapshotRepository.insert_version()` 按 `(symbol, trade_date)` 取
`MAX(version)+1` 追加，读侧（DWS/`latest()`）只看最新版本，看到的就一定是元。

只动 quote 与 financial 两类键：kline / announcement / research / risk / northbound
本单没有改它们的字段语义，键不变 ⇒ 不会被重灌一遍。

## 2. 单位改在哪

| | 厂商原值 | 归档（SQLite） | 事件 | 换算点 |
| --- | --- | --- | --- | --- |
| CN `total_market_cap` | 亿元（腾讯行情第 45 位） | **保持亿元原值** | **元** | `crawler/store.py:_quote_event` 里 `_yuan_from_yi()`，全链路仅此一次 |
| US `total_market_cap` | 按厂商：sina 绝对值 / tencent 亿 | — | 该市场基本货币绝对值 | `us_collector/collector.py:market_cap_in_base_unit()`，按 provider 取刻度 |

归档不动刻度，是为了让「换算只发生在事件层」这件事可测：
`tests/regression/test_event_units_and_fields.py` 会扫源码里 `1e8`/`100000000`
的出现次数（`{"store.py": 1, "quotes.py": 0}`），多乘一次立刻红。

## 3. 切换步骤（生产）

```
1 停采发布（采集不停）
  CN: EVENT_PUBLISH_ENABLE=false 重启 crawler/daemon
  US: tools.quote=false 或停 th-us-collector 调度
2 排空
  CN: SELECT count(*) FROM event_outbox WHERE status='pending'   -- 本地 SQLite，应为 0
  总线: XLEN fc:stockdata:events 连续两次采样不再增长
3 部署带 |g2 的版本（本 PR）
4 打开发布
5 有界重灌历史（只重灌需要修的键空间）
  谓词三件都要有，缺一个就可能扫全表：
    provider（CN='tencent'；US IN ('sina','tencent',...)）
    trade_date 窗口（默认覆盖到下游实际使用的最早快照日；见第 6 节：窗口选小了会留下亿元的最新版本）
    symbol 显式清单
  执行方式：collector/crawler 侧按清单+窗口重发事件（新键 → 新版本），
  不写任何 UPDATE，不 TRUNCATE，不重建表
6 校验（只读 SQL，第 4 节的分桶查询）
```

回滚不靠还原历史行（append-only 也不允许）：把 hub 退回旧版本只会产生**又一个**
新版本；旧事件因 `event_id` 已注册会被去重丢弃，因此「退回」不能靠重放旧事件，
只能靠发布带正确单位的新事件。这一点决定了切换只能前进，不能倒退 —— 所以第 5 步
的窗口与清单要先在 QA 跑一遍（本单已跑，见第 4 节）。

## 4. QA 演练证据（`financecoffer_qa`）

脚本：本单工作目录 `qa_cutover_rehearsal.py`（只连 QA，连库前重写并断言库名）。
demo 标的 `Q249DEMO`，厂商值取 2026-10-30 工商银行实录：`31185.55` 亿元、
`pe_ttm=8.12`、年度归母 `288278000000.0000` 元。

**BEFORE**（`dwd.stock_quote_snapshot`，按 provider 分桶）

```
{'provider': 'quot244', 'rows': 1, 'non_null': 1, 'minimum': 2000000.0000, 'maximum': 2000000.0000, 'yi_scale': 1, 'yuan_scale': 0}
{'provider': 'tencent', 'rows': 2, 'non_null': 2, 'minimum': 31185.5500,   'maximum': 31185.5500,   'yi_scale': 2, 'yuan_scale': 0}
Q249DEMO 2026-10-28 v1 [亿元代] cap=31185.5500 implied_pe=1.1E-7  implied/vendor=0.000000
Q249DEMO 2026-10-30 v1 [亿元代] cap=31185.5500 implied_pe=1.1E-7  implied/vendor=0.000000
```

`implied/vendor = 0` 就是下游 `reference`/`snapshot` 口径全断的形态：市值(亿) ÷
利润(元) 差 8 个数量级，PE 落在 1e-7 —— 只改一半（改了单位没改键，或改了键没改单位）
这条必然挂。

**有界 UPDATE 被数据库拒绝**

```
REJECTED by the append-only guard: dwd.stock_quote_snapshot is append-only:
    insert a new version instead of UPDATE
```

**旧事件重放 → 0 行**（`UNIQUE (event_id)`，`uq_quote_event`）：

```
replayed the pre-cutover event; rows inserted: 0
```

⇒ 切换后老事件即使被重放，也顶不掉修正后的最新版本。

**版本追加重灌**：`biz_key '2026-10-30|150003'` → `evt_12d95fecf9a1…`（已存在）；
`biz_key '2026-10-30|150003|g2'` → `evt_c08b419aed63…`（新）⇒ 落库 1 行：

```
rows inserted by the corrected event: 1
 v1 cap=31185.5500
 v2 cap=3118555000000.0000
second pass rows inserted: 0        -- 同一条修正事件再灌一次，幂等
```

**AFTER**

```
{'provider': 'tencent', 'rows': 3, 'non_null': 3, 'minimum': 31185.5500, 'maximum': 3118555000000.0000, 'yi_scale': 2, 'yuan_scale': 1}
Q249DEMO 2026-10-30 v2 [元代] cap=3118555000000.0000 implied_pe=10.81787372 implied/vendor=1.332250
latest(2026-10-30) total_market_cap=3118555000000.0000  == 31185.55 × 1e8  → True
```

`implied_pe=10.82` 对厂商 `pe_ttm=8.12`，比值 1.33 落在 `0.5..2.0` 的合理带内
（差异来自 TTM 与年度口径，不是量级）。

**共存不变式**：同一个 `(symbol, trade_date)` 的**最新版本**只可能是一代刻度；
历史行仍按 append-only 保留亿元刻度，但没有任何读取路径会取到它。

**窗口选小的反例（重要）**：演练里 10-28 故意不放进重灌窗口，它的最新版本仍是
`31185.5500`（亿元）。这就是第 3 步第 5 条要求先定窗口的原因 —— 未覆盖的日期不会
自己变对。

```
Q249DEMO 2026-10-28 latest total_market_cap=31185.5500 [亿元代（未修）]
Q249DEMO 2026-10-30 latest total_market_cap=3118555000000.0000 [元代]
```

演练只在 `financecoffer_qa` 做，收尾删掉 demo 标的（`rows left: 0`），
备份表 `dwd.q249_unit_backup` 留在 QA 供复核。生产库全程只读。

## 5. 生产存量规模（只读取证）

`prod_scale_sizing.py`（`set_session(readonly=True)`，连库前断言库名）在默认应用库
`financecoffer` 上的实录：

```
quote_rows: 1
quote_by_scale: ('tencent', 'yi-scale (亿元, pre-cutover)', 1, 2026-10-27, 2026-10-27)
live_rows_still_yi: ('tencent', live_yi_rows=1, live_yuan_rows=0, 2026-10-27, 2026-10-27)
financial_rows: (24, with_profit=8, 2024-09-30, 2026-06-30)
ods_quote_events: ('CN','manual','yi-scale (亿元)',1,'2026-10-27|161456')
                  ('CN','tickhub-cn','no cap',1) ('US','tickhub-us','no cap',1)
```

本机这套环境的真实修复窗口 = **1 行 quote（provider=tencent, 2026-10-27）**，
ODS 侧 1 条带市值的 CN quote 事件是 `source='manual'` 的手工投递。生产环境执行时同一
条查询给出各自的行数与日期跨度，先贴数再动手。

## 6. 与 QUOT-251（`0013`）的顺序

FinanceCoffer 的事件模型全程 `model_config = ConfigDict(extra="allow")`
（`events/models.py:86..286`），DWD writer 按字段名取值。所以：

- 本 PR 先发、`0013` 后上：`pb_mrq` / `amount_net_profit_deducted` 能过校验、进 ODS
  `raw_json`，但在 `0013` 之前**查不到列**（`dwd` 里还没这两列）。不阻塞、不报错。
- `0013` 先上、本 PR 后发：列存在但为 null，下游 `reference` 口径继续 null。
- `total_market_cap` 的单位切换**立即生效**，与 `0013` 无关 —— 这是本单真正的 gate。

校验 SQL（`0013` 之后跑）：

```sql
SELECT count(*) FILTER (WHERE pb_mrq IS NULL) AS pb_missing,
       count(*) FILTER (WHERE total_market_cap < 1e8) AS still_yi,
       count(*) AS rows
FROM dwd.stock_quote_snapshot
WHERE (symbol, trade_date, version) IN (
  SELECT symbol, trade_date, MAX(version) FROM dwd.stock_quote_snapshot GROUP BY symbol, trade_date);
```

## 7. 自查清单

- [x] CN quote 带 `pb_mrq`（>0 才发），financial 带 `amount_net_profit_deducted`（YTD，元，缺失即缺席）
- [x] `total_market_cap` 事件层为元，全链路只换算一次（源码计数测试）
- [x] US 逐厂商刻度fixture + 归一后跨厂商同量级 + 股本量级测试
- [x] 交叉字段一致性断言：市值(元) ÷ 年度归母(元) 与厂商 `pe_ttm` 同数量级
- [x] 单位审计表：`UNIT-AUDIT.md`（CN）/ `UNIT-AUDIT.md`（US）
- [x] 切换与存量：本文件，含 QA 实录、行数/量级前后对比、只读生产取证
- [ ] 端到端复验（QUOT-245 第二段）
- [ ] `0013` 落库（QUOT-251）
