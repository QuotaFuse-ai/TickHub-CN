# 金额单位审计表 —— TickHub-CN 事件旁路

QUOT-249 交付 (a)。逐个数值的「源刻度 → 事件刻度 → 是否换算 → 下游期望」，不只市值。
刻度按 2026-10-30 的线上取证确定（取证方法见文末），不按字段名猜。

契约基线：FinanceCoffer `docs/specs/event-contract.md` v0.8、`docs/specs/dws-metrics.md` §4.1
（「金额单位：元（CNY）……不做万元/亿元换算」）。

## 1. quote（provider=tencent，`crawler/store.py:_quote_event`）

| 字段 | 源刻度 | 事件刻度 | 换算 | 下游期望 |
| --- | --- | --- | --- | --- |
| `close` | 元 | 元 | 无 | `dwd.stock_quote_snapshot.close NUMERIC(20,4)`，元 |
| `total_market_cap` | **亿元**（腾讯 45 号位） | **元** | **×1e8，只在事件层一次** | `total_market_cap NUMERIC(24,4)`，元 |
| `pe_ttm` | 倍数 | 倍数 | 无 | `pe_ttm NUMERIC(20,4)` |
| `pb` | 倍数 | 倍数 | 无 | 契约未定义，FC 不消费（保留原样，兼容既有存档） |
| `pb_mrq`（新增） | 倍数（厂商 MRQ 原值） | 倍数 | 无 | `pb_mrq NUMERIC(20,4)`，`reference` 口径透传；仅 `> 0` 才发 |

存档侧 `quote_snapshot.total_market_cap` 保留**亿元原值**：SQLite 是厂商镜像，换算只发生在
事件构造这一处。重复换算由 `tests/regression/test_event_units_and_fields.py` 从两个方向夹住：
算术（事件值 ÷ 厂商值 == 1e8）与源码扫描（事件路径上比例因子只允许出现一次）。

## 2. financial（provider=eastmoney，`crawler/store.py:_financial_event`）

| 字段 | 源刻度 | 事件刻度 | 换算 | 下游期望 |
| --- | --- | --- | --- | --- |
| `amount_revenue` | 元（`TOTAL_OPERATE_INCOME`，年初至今累计） | 元 | 无 | `NUMERIC(20,4)`，元 |
| `amount_net_profit` | 元（`NETPROFIT`，缺失时退 `PARENT_NETPROFIT`） | 元 | 无 | 同上 |
| `amount_net_profit_deducted`（新增） | 元（`DEDUCT_PARENT_NETPROFIT`，年初至今累计） | 元 | 无 | `amount_net_profit_deducted NUMERIC(20,4)`；**缺失不发，绝不用归母顶替** |

## 3. kline（provider=tencent）

| 字段 | 源刻度 | 事件刻度 | 换算 | 下游期望 |
| --- | --- | --- | --- | --- |
| `open`/`high`/`low`/`close` | 元 | 元 | 无 | 元 |
| `volume` | 手 | 手（整数字符串） | 无 | 契约样例 `"12345600"` 未声明手/股；**刻度待定，见 §6-2** |
| `amount` | 厂商日 K 未提供（2026-10-30 实测 `item[6]` 缺失） | 不发 | — | 契约样例为元 |

## 4. research / announcement / risk

| 字段 | 源刻度 | 事件刻度 | 换算 | 下游期望 |
| --- | --- | --- | --- | --- |
| `target_price` | 元 | 元 | 无 | 元 |
| risk / announcement 数值项 | 无金额字段 | — | — | — |

## 5. northbound（provider=eastmoney_datacenter）

| 字段 | 源刻度 | 事件刻度 | 换算 | 下游期望 |
| --- | --- | --- | --- | --- |
| `net_buy_amount` | 百万元（`NET_DEAL_AMT`） | **万元**（`datasources/funds.py` ×100） | 已在数据源换算 | 契约样例 `"123456789.0000"` + `currency=CNY` ⇒ **元**，与本行不符，见 §6-1 |
| `deal_amount_wan` | 百万元（`DEAL_AMT`） | 万元 | 已在数据源换算 | 字段名自带「万」，与事件刻度一致 |
| `deal_num` | 笔 | 笔 | 无 | 计数，非金额 |

## 6. 已知限制与待决项

1. **北向 `net_buy_amount` 是万元，契约期望元。**本单只授权改 quote / financial 两类字段，
   不改北向；换算点也不在事件层（在数据源层已经 ×100），因此不能顺手 ×1e4。需要产品与 FC
   确认「净买入额」对外口径后再单独派单。
2. **`kline.volume` 手/股未定。**腾讯给的是手，契约样例没有声明单位；FC 只存不换算。
   与 §6-1 同为「刻度需人类确认」项。
3. **`dwd.stock_quote_snapshot` 没有币种列**（FC `db/models/dwd.py:200-234`，`currency` 只存在于
   `NorthboundFlow`）。本单不扩 FC 表结构，因此 A 股之外的金额事件缺少自证币种的落库位置：
   本期 `reference` 只承诺 A 股，US/HK 走 null + 原因码，不是缺陷。
4. 事件里所有金额一律**字符串**、4 位小数，全链路 `Decimal`（`float` 乘 1e8 会把大市值的
   尾数变成噪声，`NUMERIC(24,4)` 侧会当成精度损失拒绝入库）。

## 7. 取证方法

刻度不靠文档措辞，靠量级关系：`市值 ÷ 股价 = 股本数`，错误刻度会得到不可能的股本数。

```
# 2026-10-30 线上实测（脚本见本仓库外，结论如下）
601398 价 8.75 元，45 号位 31185.55      → ×1e8 ÷ 8.75 = 3.56e10 股（工行总股本）✓
600519 价 1392.05 元，45 号位 17401.76   → ×1e8 ÷ 1392.05 = 1.25e9 股（茅台总股本）✓
不乘 1e8 时分别是 3564 股 / 12.5 股 → 不可能，故 45 号位为亿元。
```

财务侧同一招：`601398` 2026-09-30 `revenue = 695947000000.0`，即 6959.47 亿元，与该行 9 个月
累计营业收入同量级 ⇒ 利润表金额为元；`deducted_net_profit = 284926000000.0` 与
`parent_net_profit = 285640000000.0` 是两个不相等的数，正是「不得顶替」的现实依据。

## 8. 键上的字段代次（与单位切换绑定）

单位一改，历史数据就成了错的；而 `event_id` 只哈希 `provider|data_type|symbol|biz_key`，
下游按它去重，DWD 又是 append-only（业务 UPDATE 被触发器拒绝）。因此 quote 与 financial 的
`biz_key` 末尾带代次标记 `|g2`（= 市值为元 + `pb_mrq` + `amount_net_profit_deducted`），
修正后的事件才会作为新版本落库，而不是被当成重放丢弃。kline / announcement / research /
risk / northbound 的键不变。切换步骤与 QA 实录见 [`CUTOVER-UNITS.md`](CUTOVER-UNITS.md)。

守住这条的测试：`tests/regression/test_event_units_and_fields.py`
（`test_fixed_quote_does_not_hash_to_the_stored_event_id`、
`test_generation_tag_is_a_constant_not_a_timestamp`、
`test_market_cap_and_net_profit_agree_with_the_vendor_pe`、
`test_a_single_sided_cutover_breaks_the_pe_consistency`）。

交叉字段一致性（issue 要求的「只改一半必挂」闸）：
`3118555000000.0000 元 ÷ 288278000000.0000 元 = 10.82`，厂商 `pe_ttm = 8.12`，比值 1.33 落在
合理带内；不改单位时同一算式得 `1.1e-7`。

## 9. 需要 FinanceCoffer 侧合入的契约增补（本单不改 FC）

`event-contract.md` §3.4 与 `dws-metrics.md` §4.1 的现文本只写了「金额单位：元」，没有写清
新字段与缺失语义。建议增补（逐字可用）：

```
quote.structured_fields
  total_market_cap  string 基本货币单位（A 股=元；US=美元；HK=港元）。厂商刻度差异在采集端
                           归一，消费端不得再乘任何系数。
  pb                string 厂商市净率原值（倍数）。契约不消费，仅为存档兼容保留。
  pb_mrq            string 最新报告季市净率（倍数），与 pb 同值。仅当 > 0 时出现；
                           0 / 负 / 缺失 ⇒ 字段缺席并进 quality_metrics.missing_fields。
financial.structured_fields
  amount_net_profit_deducted  string 扣非归母净利润，年初至今累计，基本货币单位。
                           缺失 ⇒ 字段缺席；消费端不得用 amount_net_profit（归母）顶替，
                           两者在存在非经常性损益时不等，顶替即一次静默的估值重述。
biz_key
  quote / financial 的键尾带字段代次标记（当前 `|g2`）。event_id 只哈希
  provider|data_type|symbol|biz_key，DWD append-only 且按 event_id 去重，因此字段单位/口径
  变化必须体现为键变化，否则修正会被当成重放丢弃。标记是常量，不随运行时间变化。
```


