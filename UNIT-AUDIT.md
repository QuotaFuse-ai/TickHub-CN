# 金额单位审计表 —— TickHub-CN 事件旁路

QUOT-249 交付 (a)。逐个数值的「源刻度 → 事件刻度 → 是否换算 → 下游期望」，不只市值。
刻度按 2026-10-30 的线上取证确定（取证方法见文末），不按字段名猜。

> 2026-10-31 QUOT-258 定稿：北向金额、K 线成交量的刻度已定，§3 / §5 的表格与 §6 的待决项
> 按结论改写，三问的选择理由、代价与派单见 §9.2–§9.4；可直接合入 FinanceCoffer 的契约文本见 §9.5。

契约基线：FinanceCoffer `docs/specs/event-contract.md` v0.8（本文件 §9.5 是其 v0.9 增量）、
`docs/specs/dws-metrics.md` §4.1（「金额单位：元（CNY）……不做万元/亿元换算」）。

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
| `volume` | 手（腾讯日 K 第 6 位） | **股**（整数字符串） | **×100，只在事件层一次** | `dwd.stock_kline_daily.volume NUMERIC(24,4)`，股；1 手 = 100 股，见 §9.3 |
| `amount` | 厂商日 K 未提供（2026-10-30 实测 `item[6]` 缺失） | 不发 | — | 契约样例为元 |

## 4. research / announcement / risk

| 字段 | 源刻度 | 事件刻度 | 换算 | 下游期望 |
| --- | --- | --- | --- | --- |
| `target_price` | 元 | 元 | 无 | 元 |
| risk / announcement 数值项 | 无金额字段 | — | — | — |

## 5. northbound（provider=eastmoney_datacenter）

| 字段 | 源刻度 | 事件刻度 | 换算 | 下游期望 |
| --- | --- | --- | --- | --- |
| `net_buy_amount` | 历史：百万元（`NET_DEAL_AMT`）；实时：万元（`dayNetAmtIn`） | **元** | 历史 ×100 → 万元，实时本就是万元（工具契约）；事件层再 ×1e4 → 元 | 契约样例 `"123456789.0000"` + `currency=CNY`，元 |
| `deal_amount`（原 `deal_amount_wan`） | 百万元（`DEAL_AMT`） | **元** | 同上一行 | 元；字段名不再携带单位，见 §9.2 |
| `deal_num` | 笔 | 笔 | 无 | 计数，非金额 |

## 6. 已知限制与待决项

1. ~~**北向 `net_buy_amount` 是万元，契约期望元。**~~ **已定稿（§9.2）：事件为元，数据源与
   工具契约保持万元，换算只在事件层。**
2. ~~**`kline.volume` 手/股未定。**~~ **已定稿（§9.3）：事件为股（1 手 = 100 股），归档与
   工具保持厂商的手。**
3. ~~**`dwd.stock_quote_snapshot` 没有币种列。**~~ **已定稿（§9.4）：本期不改表结构；契约约束
   与扩表触发条件见 §9.4。**（同一张表也没有 `market` 列，这是比币种更前置的缺口。）
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
证据库按 2026-10-30 13:10 裁定重定：真值库 `financecoffer_qa_245` 只读，演练跑在它的私有
副本 `financecoffer_qa_249`（`CREATE DATABASE … TEMPLATE financecoffer_qa_245`）上，
共享 `financecoffer_qa` 已是 QUOT-244 的造数库，不作为真值。

守住这条的测试：`tests/regression/test_event_units_and_fields.py`
（`test_fixed_quote_does_not_hash_to_the_stored_event_id`、
`test_generation_tag_is_a_constant_not_a_timestamp`、
`test_market_cap_and_net_profit_agree_with_the_vendor_pe`、
`test_a_single_sided_cutover_breaks_the_pe_consistency`）。

交叉字段一致性（issue 要求的「只改一半必挂」闸）：
`3118555000000.0000 元 ÷ 288278000000.0000 元 = 10.82`，厂商 `pe_ttm = 8.12`，比值 1.33 落在
合理带内；不改单位时同一算式得 `1.1e-7`。

## 9. FinanceCoffer 契约增补与 QUOT-258 三问定稿

### 9.1 QUOT-249 已交付字段（原草案，可合入）

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

### 9.2 定稿一：北向金额统一为元

**结论：事件里一律元——`net_buy_amount` 与 `deal_amount` 都是元；`datasources/funds.py` 与
`northbound_flow` 工具契约保持万元。**

为什么：

1. 契约 §3.7 的样例 `"123456789.0000"` 只有按元才成立：12.3 亿元/日是北向单日净买入的正常
   量级；按万元是 1.2 万亿元/日，超过全市场单日成交额，不可能是真的。
2. 事件信封对所有 `data_type` 只有一个金额刻度（`dws-metrics.md` §4.1「金额单位：元，
   不做万元/亿元换算」）。北向不是例外——一处例外就是下一次静默的 ×1e4。
3. 数据源不能改：`funds.py` 的出参 meta 写着 `unit: "万元"`，CLI、归档页与 `skills/` 示例都
   按万元消费，归档本身是厂商镜像。所以换算点仍然只有一个：**事件层
   `crawler/store.py::_northbound_event`**，与 quote 的 `_yuan_from_yi()` 同规则。

代价与改动点：

- 新增 `_yuan_from_wan()`（`Decimal × 10000`，四位小数），`net_buy_amount` 与成交额各调用一次。
- `deal_amount_wan` 更名为 `deal_amount`（同值，元）。理由：同一信封里不允许两种金额刻度，而
  字段名是唯一能藏单位的地方。FC 的 `NorthboundFields` 是 `extra="allow"`、DWD 没有这一列
  （`db/models/dwd.py:269-288`），改名对 FC 无破坏；若后端架构师确认存在外部消费方读旧名，
  退一步保留 `deal_amount_wan` 并在 §3.7 规则行显式标注「万元」——这是唯一允许的例外。
- **不加代次标记**，`biz_key` 仍是 `trade_date|channel`。FC 的北向写入是 `insert_once`
  （`db/repositories/dwd.py:114-117`，键 `(trade_date, channel)`）：换键只会在 ODS 留下同一天
  两条不同量级的事件，DWD 仍只认第一条，等于制造双份真相而不是修正。新日期自然用新单位。
- 存量：`NET_DEAL_AMT` 自 2024-08-19 起上游停发，爬虫只回看 250 个交易日
  （`_NORTHBOUND_HISTORY_DAYS`），历史路径必然为 null；带值的行只可能来自盘中实时快照。
  上线前按 `SELECT channel, trade_date, net_buy_amount FROM dwd.northbound_flow
  WHERE net_buy_amount IS NOT NULL` 盘点；若 >0，走一次定向人工修数（本期不做，属生产写操作）。
- 部署当天已发布的万元事件不会自己变对（同日同键被去重），次日恢复；当天若必须正确，只能按该
  `(trade_date, channel)` 定向补发，并接受 DWD 只认首行的事实。

验收断言（子单 A）：

- `net_inflow = 123456.78`（万元）→ 事件 `net_buy_amount == "1234567800.0000"`（×1e4，四位小数）。
- 负值原样保留（净卖出是真实状态），`None` 仍缺席、不补 0。
- 源码扫描：北向换算因子只允许出现在 `store.py::_yuan_from_wan()` 一处；`funds.py` 不得出现 `10000`。
- 集成测试 fixture 换成真实量级（现在的 `123456789.0` 万元是 1.2 万亿元，会把单位错误掩盖掉）。

**是否需要改代码：是**（TickHub-CN，事件层 + 测试）。这一条不能用文档约束替代：契约样例与
代码现在相差 1e4，不改就是持续产出错数。

### 9.3 定稿二：K 线成交量统一为股

**结论：A 股 1 手 = 100 股；事件 `volume` 一律股（整数字符串）。归档与 `tencent_kline`
工具保持腾讯原值「手」。**

为什么：

1. 契约 §3.3 的样例 `"12345600"` 按股是 1234.56 万股/日，正常；按手是 12.3 亿股/日，比工行
   2026-10-30 全天的 4.32 亿股还多近 3 倍——样例只有按股才落在常见量级。
2. US 侧本来就是股（US `UNIT-AUDIT.md` §2）。手与股混用会让跨市场比较成交活跃度差 100 倍，
   比字段缺失更危险——数字看着正常，结论错 100 倍。
3. 换算成本极低：FC 只存不算（`dwd/writer.py:215-217`、`db/models/dwd.py:148`），DWS 没有任何
   公式消费 volume；LadderStock 读的是 MCP 工具而不是事件，不受影响。

代价与改动点：

- 事件层 `store.py::_kline_fields` 新增 `_shares_from_lots()`（`Decimal × 100`），输出整数字符串；
  归档 SQLite 保留手（厂商镜像，与 quote 的亿元原值同一规则）。
- `biz_key` 从 `trade_date|period|adjust` → `trade_date|period|adjust|g2`。**必须换键**：FC 的
  K 线走 `insert_version`（`dwd/writer.py:205`），换键后修正值落成 v2、读侧取最新版本；不换键则
  被 `event_id` 去重丢弃，修正永远到不了仓。
- 工具契约不改：`tencent_kline` 镜像厂商，`skills/tickhub-cn/examples/quote.md` 已标注「手」。

验收断言（子单 A）：

- 601398 2026-10-30：厂商 `4315746` 手 → 事件 `"431574600"`；`biz_key` 以 `|g2` 结尾，同日
  重发 `event_id` 稳定（常量，不是时间戳）。
- 量级夹层（两条独立旁证）：`431574600 股 × 8.75 元 = 3.776e9`，腾讯同笔成交额
  `3760466142 元`，差 0.4%（VWAP 差异）；换手率 0.16% × 流通股 2696 亿 ≈ 431M 股。
- 反例：不换算时 `volume × close` 比成交额小 100 倍，测试必须抓得到。
- 源码扫描：`store.py` 的 K 线路径只允许一处 `100` 因子；`market.py`（工具解析）不得出现换算。

**是否需要改代码：是**（TickHub-CN，事件层 + 测试 + 键代次）。

### 9.4 定稿三：US/HK 没有币种列 → 本期不改表，但必须补市场闸

**结论：本期不改表结构。US/HK 金额在 DWD 里没有币种可依，按「不可用于任何金额计算」处理；
同时把「非 A 股不得进入估值口径」变成代码闸，而不是只写在文档里。**

取证（2026-10-31）：

- `dwd.stock_quote_snapshot`（`db/models/dwd.py:200-234`）既没有 `currency`，也没有 `market`；
  `currency` 只存在于 `dwd.northbound_flow`（`:284`）。US 事件其实带了 `currency`/`market`
  （`us_collector/collector.py:489-491`），但 `_write_quote`（`dwd/writer.py:230-262`）不落这两列，
  落库后不可自证。
- `reference`/`snapshot` 的符号域来自 DWD 里已有的 symbol（`dws/builder.py:681-700`），**没有任何
  市场过滤**。`symbol` 是唯一的市场判别依据（6 位数字 = A 股，字母 = US ticker，
  `query/service.py:54-68`）。
- 货币信息在事件里有、在 DWD 里没有；唯一能防止误用的地方是 DWS 的符号域。

本期约束（写进契约，不改表）：

1. 三张 DWD 表（`stock_quote_snapshot` / `stock_kline_daily` / `stock_financial_snapshot`）的
   金额列**只按 A 股口径理解为元**；非 A 股行不得参与估值、跨市场比较或对外展示。
2. US/HK 的 `reference`/`snapshot` 输出 **null + 原因码**（`currency_unknown` /
   `unsupported_market`），这是预期行为，不是缺陷。
3. DWS `_targets` 增加 A 股闸：`build --all` 只枚举 6 位数字 symbol；被跳过的符号进
   `BuildReport` 的可解释原因，不静默丢弃。

扩表触发条件（满足任意一条就必须做，不受本期约束）：

- 任一 US/HK 标的进入 `reference`/`snapshot` 的符号域；
- 任一消费方需要跨市场比较市值、成交额、PE/PB；
- 对外接口或看板展示 US/HK 金额且不附带市场上下文；
- FC 把 `amount` / `volume` 纳入任何公式。

扩表时要动的字段（预估成本，写在这里免得重新考古）：三张表各加
`currency CHAR(3) NOT NULL DEFAULT 'CNY'` 与 `market CHAR(2)`；存量 A 股行回填 `CNY`/`CN`；
`quote` 的 `close`/`total_market_cap`、`kline` 的 `open/high/low/close/amount`、`financial` 的
`amount_*` 全部受影响；`volume` 跨市场同为股，不受币种影响。

**是否需要改代码：表结构不改（本期）。** DWS 市场闸是代码改动，落子单 B（低风险防御，不碰表）。

### 9.5 可直接合入 FinanceCoffer 的契约文本

（§9.1 的 quote/financial 段落继续有效，以下是 v0.9 增量，逐字可用。）

```text
[event-contract.md §2 biz_key]
  K 线   = trade_date|period|adjust|g2
  quote  = trade_date|ts|g2
  北向   = trade_date|channel              （该表 insert_once，换键无收益，见 §9.2）
  尾缀 g2 = 字段代次：市值为元 + pb_mrq + 扣非归母（quote/financial），或成交量由手改为股
            （kline）。代次是常量，不随运行时间变化；event_id 只哈希
            provider|data_type|symbol|biz_key，DWD 按 event_id 去重，所以单位/口径变化必须
            体现为键变化，否则修正会被当成重放丢弃。

[event-contract.md §3.3 kline]
  volume  string  成交量，单位**股**。A 股由采集端按 1 手 = 100 股换算，US 本来就是股；
                  消费端不得再乘 100，也不得按行业惯例理解为手。

[event-contract.md §3.4 quote]
  total_market_cap / close 为**基本货币单位**（A 股=元，US=美元，HK=港元），换算在采集端完成。
  structured_fields 里的 currency/market 仅供追溯：dwd.stock_quote_snapshot 没有这两列，
  非 A 股行的金额没有币种可依，不得参与任何估值或跨市场比较。

[event-contract.md §3.7 northbound]
  net_buy_amount string  当日净买入额，单位**元（CNY）**。上游 2024-08-19 起停止披露时字段
                         缺席，不补 0、不外推。
  deal_amount    string  当日成交额，单位**元（CNY）**。旧名 deal_amount_wan（万元）作废，
                         不再发送。
  currency       string  固定 "CNY"。
  biz_key        trade_date|channel
  消费端注意：dwd.northbound_flow 以 (trade_date, channel) 插入一次、不可变；同一天换单位后
  重新发布的事件不会覆盖旧行。

[dws-metrics.md §4.1 金额单位行]
  元（CNY），与 DWD amount_* 原样一致，不做万元/亿元换算、不做汇率换算。该口径只适用于 A 股
  （market=CN）；US/HK 行缺少币种列，reference/snapshot 一律 null + 原因码
  （currency_unknown / unsupported_market）。

[event-contract.md §7 G-02]
  kline 成交量单位已定：股（1 手 = 100 股）；northbound 金额单位已定：元。两项均为 QUOT-258
  产品定稿（2026-10-31），TickHub 侧按此实现，契约不再保留「待确认」。
```

### 9.6 派单

- **子单 A（TickHub-CN）**：事件层单位统一——北向元化（§9.2）与 K 线股化（§9.3），含测试与键代次。
- **子单 B（FinanceCoffer）**：契约 v0.9（§9.5 逐字合入）+ DWS 市场闸（§9.4）。
- **不派单**：表结构变更（§9.4 明确本期不做）、存量北向修数（先盘点，>0 再单独议）。


