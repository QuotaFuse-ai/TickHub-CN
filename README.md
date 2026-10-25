<h1 align="center">TickHub-CN</h1>

<p align="center">
  <b>A股数据枢纽</b>
</p>

<p align="center">
  A 股全栈数据 · 14 层工具 · 124 个 MCP 工具 + 25 个兼容别名
</p>

<p align="center">
  行情 K 线 · 财报 · 资金面 · 公告 · 研报 · 宏观 · 指数 · 期货 · 期权 · 可转债
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white" alt="Python">
  <a href="https://github.com/QuotaFuse-ai/TickHub-CN/stargazers"><img src="https://img.shields.io/github/stars/QuotaFuse-ai/TickHub-CN?style=social" alt="Stars"></a>
  <br>
  <img src="https://img.shields.io/badge/layers-14-2ea44f.svg" alt="Layers">
  <img src="https://img.shields.io/badge/MCP%20tools-149-2ea44f.svg" alt="Tools">
  <img src="https://img.shields.io/badge/auth-zero-success.svg" alt="Zero Auth">
</p>

---

<p align="center">
  <a href="#zero-key">30 秒上手</a> ·
  <a href="#compare">为什么选它</a> ·
  <a href="#features">特性</a> ·
  <a href="#architecture">架构</a> ·
  <a href="#quickstart">完整安装</a> ·
  <a href="#clients">接入方式</a> ·
  <a href="#skill">Skill</a> ·
  <a href="#layers">数据覆盖</a> ·
  <a href="#examples">使用示例</a> ·
  <a href="#faq">FAQ</a> ·
  <a href="https://github.com/QuotaFuse-ai/TickHub-CN/releases">版本发布</a>
</p>

TickHub-CN（A股数据枢纽）只做一件事：**把 A 股数据变成模型可以直接调用的结构化工具**。
产品围绕一条规则构建 —— **模型报告的每一个数字都必须来自一次工具调用**。
行情、财报、资金、公告、研报、宏观、指数、期货、期权、可转债，全部由服务端抓取、
归一化、缓存，并带上来源与时间戳，而不是让模型回忆或估算。

**只想先看它能不能出数？** 跳到 [30 秒上手](#zero-key)：两条命令，零密钥，直接拿到浦发银行的实时价。
**在想为什么不用 akshare 或自己调接口？** 看 [对比表](#compare)，一张表说清取舍。

- 124 个规范工具 + 25 个兼容别名 = **149 个 MCP 名称**，分 14 层。
- 核心数据源**不需要 API Key、不需要账号、不需要验证码**；可选源用环境变量开启。
- Redis 可选：没有 Redis 时自动降级为进程内缓存，并以 `QFSD-2001` 提示，不会失败。
- 上游失败自动切换备用源；批量调用逐条报告失败，不吞掉成功项。
- 每个响应都带 `source / fetched_at / cache_hit / trace_id`，支持 `data_as_of` 时点回看。

---

<a id="zero-key"></a>

## 30 秒上手（零密钥）

<p align="center">
  <img src="https://img.shields.io/badge/%E9%9B%B6%E5%AF%86%E9%92%A5-%E6%97%A0%E9%9C%80%E6%B3%A8%E5%86%8C%20%C2%B7%20%E6%97%A0%E9%9C%80API%20Key-critical?style=for-the-badge" alt="零密钥：无需注册 · 无需 API Key">
</p>

**这一步没有任何密钥。** 不注册、不申请 AppID、不填 token、不配代理，复制下面两行就出数：

```bash
pip install https://github.com/QuotaFuse-ai/TickHub-CN/releases/download/v0.1.1/tickhub_cn-0.1.1-py3-none-any.whl
th-cn stock info 600000
```

第二条命令的真实返回（2026-10-26 实网抓取，节选）：

```json
{
  "ok": true,
  "data": { "code": "600000", "name": "浦发银行", "pe": 6.2, "pb": 0.42, "total_market_cap": 317405000000.0 },
  "meta": { "source": "tencent", "fetched_at": "2026-10-26T01:02:10+08:00", "cache_hit": false, "confidence": "A", "trace_id": "64f6f897e10c4f82" }
}
```

同一套工具，换个名字就能取别的东西：

```bash
th-cn tool call tencent_quote --params '{"codes":"sh600000"}' --fields code,name,price,change_pct
th-cn tool call tencent_kline --params '{"code":"sh600000","period":"daily","limit":5}'
th-cn tool list --help        # 149 个名称都在这里
th-cn doctor                  # 环境、缓存后端、数据源体检
```

> 为什么能这么快：**① 零密钥** —— 安装完就结束，没有任何登录/授权步骤；
> **② 行情走腾讯源** —— 不封 IP，不需要代理；**③ Redis 可选** —— 没装 Redis 自动用进程内缓存。
>
> PyPI 尚未发布，所以安装地址指向 GitHub Release（版本更新后文件名里的版本号会变，
> 以 [Releases](https://github.com/QuotaFuse-ai/TickHub-CN/releases) 页为准）。
> 想从源码装、带管理台或开发依赖，见 [完整安装](#quickstart)。

---

<a id="compare"></a>

## 为什么选它：对比 akshare / 直接调 API

一句话：**akshare 给你更多接口，直接调 API 给你更多自由，TickHub-CN 给你「模型不写代码也能取到带出处的数」。**

| 维度 | TickHub-CN | akshare | 直接调东财 / 腾讯等接口 | Tushare / 聚宽等 Key 型 API |
| --- | --- | --- | --- | --- |
| 调用方式 | MCP 工具（模型直接调）、CLI、Python，共用一套返回 | Python 函数，需要自己写脚本 | 自己拼 URL、带 UA/cookie、自己解析 | SDK + token，仍然要写代码 |
| 鉴权 | **零密钥**，装完即用 | 零密钥 | 零密钥，但签名与请求头要自己维护 | 注册 token，部分接口按积分/付费 |
| 返回结构 | 统一 envelope：`ok / data / meta{source, fetched_at, confidence, trace_id}` | 每个函数一套 DataFrame，字段随源变化 | 原始 JSON，字段随源站改版而变 | 统一，但有版本差异 |
| 稳定性处理 | 内置分组限流、熔断、多源切换、旧缓存兜底、代理池 | 基本没有，失败需自己重试 | 全部自己扛（封 IP、字段改名） | 官方保障 |
| 覆盖广度 | 149 个名称 / 14 层，聚焦 A 股高频场景 | 上千个接口，广度最大 | 取决于愿意逆向多少 | 偏基本面与行情 |
| 部署 | 一个 wheel，Python 3.11+，无需浏览器 | `pip install` + pandas | 无需依赖，但要自己写爬虫 | 需要 SDK 与网络鉴权 |
| 许可 / 成本 | Apache-2.0，免费 | MIT，免费 | 免费，但维护成本高 | 免费额度 + 付费/积分 |
| 最适合 | AI Agent、自动化取数、不想写爬虫 | 数据探索、研究、需要广度 | 深度定制、只取一两个接口 | 需要 SLA 或合规授权的团队 |

**边界也写清楚**：上游同样是公开接口，源站改版仍要跟进；覆盖广度不如 akshare；
不是交易所授权行情，不建议用于合规披露或实盘交易决策。
选它的理由只有一条 —— **把「取数」从写代码变成一次工具调用，并且每个数字都带来源和时间。**

---

## 目录

- [30 秒上手（零密钥）](#zero-key)
- [为什么选它（对比）](#compare)
- [特性](#features)
- [架构](#architecture)
- [快速开始（完整安装）](#quickstart)
- [接入方式（MCP / CLI / Python）](#clients)
- [Skill 用法](#skill)
- [数据覆盖（14 层，149 个名称）](#layers)
- [返回结构与通用参数](#envelope)
- [使用示例](#examples)
- [代理配置（单代理与代理池）](#proxy)
- [缓存与降级](#cache)
- [管理台（Admin Console）](#admin)
- [离线爬虫](#crawler)
- [配置项](#config)
- [错误码](#errors)
- [FAQ](#faq)
- [验证](#verify)
- [文档](#docs)
- [开发](#dev)
- [免责声明](#disclaimer)

---

<a id="features"></a>

## 特性

| 能力 | 说明 |
| --- | --- |
| 四套入口 | MCP stdio / MCP Streamable HTTP / CLI `th-cn` / Python `StockDataClient`，返回同一套 envelope |
| 149 个 MCP 名称 | 124 个规范工具 + 25 个历史别名，`tools/list` 一次返回 |
| 14 层数据 | 行情、财报、盘面、资金、宏观、公告、指数、期货、事件、研报、舆情、期权、可转债、估值 |
| 时点回看 | 任意工具传 `data_as_of`，丢弃该时点之后才发布的记录 |
| 字段投影 | 全局 `fields` 选择列；`tier=brief/medium/full` 三档预设 |
| 缓存分层 | `realtime` 60s / `semi_static` 24h / `static` 7d / `health` 120s；Redis 或进程内 |
| 代理池 | 认证代理、健康检查、轮询切换、失败跳过、全挂时直连兜底（`QFSD-5001`） |
| 内置限流 | 东财 `push2` 端点分组限流 + 熔断 + 旧缓存兜底，避免被封 IP |
| 可视化管理台 | 数据源、缓存、代理池、字段映射、MCP 调试、爬虫、日志、审计，一个页面 |
| 可观测 | 结构化错误码、`trace_id`、来源健康探测、日志落 stderr（stdout 只出数据） |

---

<a id="architecture"></a>

## 架构

```
th-cn (CLI)  ─┐
              ├─► 工具注册表（124 tools / 14 layers / 25 aliases）
MCP stdio ────┤        │
MCP HTTP  ────┤        ├─► 缓存（Redis 可选 → 进程内降级）
Python API ───┘        ├─► 限流 / 熔断 / 代理池 / 重试
                       └─► 数据源适配器
                             腾讯 · 百度 · 新浪 · 东财 · 巨潮 ·
                             上交所/深交所/北交所 · 中债 · 同花顺 ·
                             通达信(tdx) · 财联社 · 华尔街见闻 · 央视网
```

进程模型（`th-cn start-all`）：**两个进程、一个端口**。

| 进程 | 职责 | 端口 |
| --- | --- | --- |
| 主进程 | MCP Streamable HTTP 服务，端点 `http://127.0.0.1:8000/mcp` | 8000 |
| 子进程 | 定时爬虫（盘后预热归档），不开监听端口 | 无 |

主进程监管子进程：崩溃自动重启；5 分钟内连续 3 次崩溃后停止重启并告警，但 MCP 继续服务；
`SIGINT` / `SIGTERM` 先优雅关闭子进程，再关闭 HTTP。

数据源优先级按「封 IP 风险」排列：

1. **腾讯 / 交易所官方 / 巨潮** —— 实时行情、K 线、公告首选，不封 IP。
2. **新浪 / 百度 / 通达信** —— 备用源，主源超时或为空时接管。
3. **东方财富** —— 只用于独有数据（板块、龙虎榜、涨停池、资金流、财报等），
   内置 `push2` 分组限流、熔断与旧缓存兜底，熔断期间走非 push2 替代源。
4. **mootdx** —— 2026-09 起行情命令失效，只用于财务与 F10，且需 `.[sources-legacy]`。

---

<a id="quickstart"></a>

## 快速开始（完整安装）

**3 步，2 分钟。** 只想要最快那条路？[30 秒上手](#zero-key) 两条命令即可。

```bash
# 1. 安装（仓库方式）
git clone https://github.com/QuotaFuse-ai/TickHub-CN.git
cd TickHub-CN
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -e ".[dev]"              # 也可直接装 Release 里的 wheel，见 30 秒上手

# 2. 自检（环境 + 缓存后端 + 数据源）
th-cn doctor

# 3. 第一次取数
th-cn stock info 600000
th-cn kline get 600000 --period daily --limit 20
```

> Windows 下如果 `th-cn` 不在 PATH，直接用虚拟环境里的
> `.venv\Scripts\th-cn.exe`，或 `python -m tickhub_cn.cli.main ...`。

可选依赖按需安装：

| 扩展 | 内容 | 用途 |
| --- | --- | --- |
| `.[admin]` | fastapi / uvicorn | 管理台（可选，默认关闭） |
| `.[sources]` | akshare / baostock / pandas / xlrd / openpyxl | 宏观、估值历史、ST 历史等可选源 |
| `.[sources-legacy]` | mootdx | 仅 `stock_f10_tips`；会钉住 `httpx<0.26`、`tenacity<9`，默认不装 |
| `.[notes]` | pypdf / pdfminer.six | 财报附注 PDF 正文摘录 |
| `.[docx]` | python-docx | 公告/研报 Word 解析 |
| `.[db]` | sqlalchemy + MySQL/PostgreSQL 驱动 | 日志落库 |
| `.[dev]` | pytest | 开发与测试 |
| `.[all]` | admin + db + sources | 一次装齐 |

一键盘后服务（MCP + 定时爬虫）：

```bash
th-cn start-all --host 127.0.0.1 --port 8000
# 端点：http://127.0.0.1:8000/mcp
```

`--crawler auto`（默认）只在 `OFFLINE_CRAWLER_ENABLE=true` 时启动爬虫子进程；
`on` 强制启动，`off` 只跑 MCP。

---

<a id="clients"></a>

## 接入方式（MCP / CLI / Python）

### MCP stdio

客户端自己拉起子进程，最适合桌面客户端。**stdout 只承载协议帧**，日志全部走 stderr。

```json
{
  "mcpServers": {
    "tickhub-cn": {
      "command": "th-cn",
      "args": ["mcp", "stdio"]
    }
  }
}
```

如果客户端找不到 `th-cn`，写绝对路径（例如虚拟环境里的 `...\Scripts\th-cn.exe`），
或改成 `"command": "python", "args": ["-m", "tickhub_cn.cli.main", "mcp", "stdio"]`。

### MCP Streamable HTTP

先起服务，再让客户端连接 `/mcp`：

```bash
th-cn start-all --host 127.0.0.1 --port 8000
# 或只起 MCP（不带爬虫子进程）
th-cn mcp http --host 127.0.0.1 --port 8000
```

```json
{
  "mcpServers": {
    "tickhub-cn": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

> HTTP 传输本身**没有鉴权**，只监听回环地址，或放在带认证的反向代理之后。
> 该端点**不是 REST**：浏览器 `fetch()` 直接调用会失败，需要 MCP SDK，
> 或使用仓库自带的 `web/` 网关。

### CLI 直接调用

```bash
th-cn stock info 600000
th-cn kline get 600000 --period daily --adjust qfq --limit 120
th-cn finance report 600000 --granularity income
th-cn tool list --layer funds
th-cn tool call tencent_quote --params '{"codes": ["600000", "000001"]}'
```

stdout 只输出数据（JSON/table/csv/yaml），日志、告警走 stderr，所以
`th-cn ... > data.json` 永远得到干净数据。全局参数：
`--output json|table|csv|yaml`、`--fields`、`--tier`、`--no-cache`、`--refresh-cache`、
`--timeout`、`--retries`、`--quiet`、`--verbose`。

### Python 直接调用

```python
from tickhub_cn import StockDataClient

client = StockDataClient()                       # 导入本身不建连接、不开端口
quote = client.tencent_quote("600000")           # 属性调用
kline = client.call("tencent_kline", {"code": "600000", "period": "daily", "limit": 60})

print(quote["data"][0]["price"])
print(len(client.tools()), client.layers())
```

`client.call(name, params)` 与 MCP `tools/call` 完全等价；`client.tools()` 返回 149 个名称。

### 客户端矩阵

「兼容」指**协议兼容**，不是逐个客户端做过认证。stdio 与 Streamable HTTP 两条传输
在仓库内有真实协议帧测试（`initialize → tools/list → tools/call`）。

| 客户端 | 接入方式 | 状态 |
| --- | --- | --- |
| Claude Code | `claude mcp add tickhub-cn -- th-cn mcp stdio`；HTTP 用 `claude mcp add --transport http tickhub-cn http://127.0.0.1:8000/mcp` | stdio / HTTP 均适用 |
| Claude Desktop | `claude_desktop_config.json` 里的 `mcpServers` 块 | stdio |
| Cursor / Cline / Zed | 客户端自己的 `mcpServers` 配置 | stdio |
| Codex CLI | `~/.codex/config.toml` 的 `[mcp_servers.tickhub-cn]` | stdio |
| OpenClaw / WorkBuddy / Muse / Grok Bot | 有 MCP 入口就用 `mcpServers` 或 URL；没有则加载 `skills/tickhub-cn/SKILL.md` 并调 CLI | 协议兼容，**未逐客户端实机冒烟** |
| 任意 OpenAI function-calling 客户端 | 用 `web/gateway.py` 或自写适配器转发到 `/mcp` | 需要适配层 |

验收标准只有一条：`tools/list` 返回 149 个名称，`tencent_quote` 返回 `ok: true`。

---

<a id="skill"></a>

## Skill 用法

`skills/tickhub-cn/` 是一个 Agent 技能包（Markdown，不执行代码），作用是告诉模型
**什么时候该取数、取哪个工具、拿到空值或降级结果时怎么办**。

```
skills/tickhub-cn/
├── SKILL.md                  # 触发条件、前置检查、核心规则
├── references/
│   ├── tools.md              # 149 个名称的完整参数表（脚本生成，勿手改）
│   ├── cli.md                # 命令行用法
│   ├── envelope-and-errors.md
│   └── sources.md            # 数据源、限流与降级
└── examples/
    ├── quote.md  kline.md  finance.md  research-workflow.md
    ├── mcp-config.json
    └── python_client.py
```

两种加载方式：

1. **作为技能目录**（推荐）：把整个 `skills/tickhub-cn/` 复制到所用 Agent 的技能目录，
   例如 Claude Code 的 `~/.claude/skills/tickhub-cn/`，重启会话后按名字 `tickhub-cn` 触发。
2. **作为项目上下文**：不复制，直接把 `SKILL.md` 提供给 Agent；需要参数细节时再补
   `references/tools.md`。

Skill 只是检索规范，**数据仍然必须来自 MCP 工具或 `th-cn` 命令**。
工具目录变化后重新生成引用表：

```bash
python scripts/gen_tool_catalog.py --format skill --out skills/tickhub-cn/references/tools.md
```

CI 会重跑生成并比对，所以不要手改 `references/tools.md`。

---

<a id="layers"></a>

## 数据覆盖（14 层，149 个名称）

| 层 | 工具数 | 覆盖内容 |
| --- | --- | --- |
| [行情 quotes](#layer-quotes) | 13 | 实时价、PE/PB、K 线（日/周/月、1~60 分钟、复权）、逐笔、ETF、指数、全市场日线 |
| [财报 financials](#layer-financials) | 13 | 三大报表、财务指标、勾稽校验、主营构成、十大股东、估值历史、F10、附注定位 |
| [盘面 market](#layer-market) | 23 | 涨停/跌停/炸板/昨涨停、龙虎榜、板块成员、市场概览、情绪、异动、ST、热榜 |
| [资金 funds](#layer-funds) | 16 | 个股/板块/排行资金流、北向、两融、大宗、股东户数、分红、筹码、ETF 份额 |
| [宏观 macro](#layer-macro) | 12 | GDP、CPI、PMI、货币供应、社融、LPR、利率走廊、Shibor、国债收益率、日历 |
| [公告 announcements](#layer-announcements) | 9 | 巨潮/深交所公告、风险事件、财联社、华尔街见闻、央视网、个股/全球新闻 |
| [指数 index](#layer-index) | 10 | 成分、权重、估值、指数 K 线、交易日历、申万行业指数与分类变迁 |
| [期货 futures](#layer-futures) | 7 | 国内期货实时/日线/K 线、持仓排名、商品与股指期权、A50、上海金 |
| [事件 event](#layer-event) | 7 | 业绩预告、机构调研、增减持、回购、股权质押、解禁、新股申购 |
| [研报 research](#layer-research) | 6 | 个股/行业研报、PDF 地址、一致预期 EPS、iwencai 自然语言搜索、新浪备用源 |
| [舆情 sentiment](#layer-sentiment) | 3 | 互动易、上证 e 互动、个股热门概念 |
| [期权 option](#layer-option) | 3 | ETF 期权合约链、希腊字母与 IV、T 型报价 |
| [可转债 bond](#layer-option) | 1 | 全市场可转债条款、转股价值、溢价率、上市/摘牌状态 |
| [估值 valuation](#layer-option) | 1 | 前瞻 PE、PEG、PE 消化年限（一致预期 EPS + 实时价） |

<!-- 行情 -->

<a id="layer-quotes"></a>

### 行情层（实时，不封 IP）

<details open>
<summary><b>13 个工具</b> — 实时价 / K 线 / 逐笔 / ETF / 指数 / 全市场日线</summary>

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `tencent_quote` | `codes` | 腾讯实时行情：价、PE、PB、换手、市值（支持多代码） |
| `tencent_quote_batch` | `codes`, `max_codes` | 批量实时行情，逐条报告失败 |
| `tencent_market_snapshot` | `codes` | 指数 / ETF / 个股一次快照 |
| `tencent_kline` | `code`, `period`, `adjust`, `limit`, `start`, `end` | 日/周/月 K 线，前复权 / 后复权 |
| `tencent_minute` | `code`, `period`, `limit` | 1 / 5 / 15 / 30 / 60 分钟 K 线 |
| `tencent_ticks` | `code`, `page`, `full` | 当日逐笔成交（沪深个股 + ETF，不含北交所） |
| `sina_adjust_factor` | `code` | 新浪前复权因子历史 |
| `apply_adjust` | `code`, `closes`, `factors` | 按因子把原始收盘价换算为前复权序列（纯规则计算） |
| `baidu_kline_with_ma` | `code`, `period`, `limit` | 百度 K 线（含 MA5/10/20），备用源 |
| `tdx_daily_package` | `date`, `market`, `download_dir` | 通达信盘后包：全市场当日日线（含成交额） |
| `etf_quote` | `codes` | ETF 实时行情（价、量、额、溢价输入） |
| `index_quote` | `codes` | 指数实时行情（沪 / 深 / 北） |
| `bse_quote_backup` | `trade_date`, `code`, `max_pages` | 北交所收盘快照与五档（官方源） |

</details>

<!-- 财报 -->

<a id="layer-financials"></a>

### 财报层

<details open>
<summary><b>13 个工具</b> — 三大报表 / 指标 / 勾稽 / 股东 / 估值历史 / F10 / 附注</summary>

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `stock_profile` | `code` | 个股概况：名称、行业、市值、流通股、PE/PB |
| `eastmoney_financial_report` | `code`, `statement`, `periods`, `report_date` | 资产负债表 / 利润表 / 现金流量表 |
| `eastmoney_financial_all` | `code`, `periods` | 三大报表 + 关键指标一次取回 |
| `eastmoney_finance_indicators` | `code`, `periods` | EPS、BPS、营收、净利、ROE 等关键指标 |
| `finance_crosscheck` | `code`, `report_date`, `tolerance`, `strict` | 勾稽校验（资产 = 负债 + 权益等） |
| `main_business` | `code`, `periods` | 主营构成（按行业 / 产品 / 地区） |
| `top_shareholders` | `code`, `holder_type`, `periods` | 十大股东 / 十大流通股东及持仓变化 |
| `cninfo_financial_notes` | `code`, `year`, `limit`, `extract`, `chars` | 定位年报 / 审计报告中的财务报表附注，可摘录正文 |
| `sina_financial_report` | `code`, `statement`, `periods` | 新浪财报（备用源） |
| `baostock_stock_basic` | `code` | 上市 / 退市日期与状态 |
| `baostock_valuation_history` | `code`, `start`, `end` | PE / PB / PS / PCF / 换手率历史（不含北交所） |
| `baostock_st_list` | — | 历史 ST / *ST 记录 |
| `stock_f10_tips` | `code` | 通达信 F10 最新提示（需 `.[sources-legacy]`，按需懒加载） |

</details>

<!-- 盘面 -->

<a id="layer-market"></a>

### 盘面层

<details open>
<summary><b>23 个工具</b> — 涨跌停池 / 龙虎榜 / 板块 / 情绪 / 异动 / ST / 热榜</summary>

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `market_overview` | — | 主要指数点位 + 涨跌家数 |
| `market_sentiment` | `date` | 由涨停 / 炸板 / 跌停池推导的短线情绪指标 |
| `limit_up_pool` | `date` | 涨停池，含连板数与封板时间 |
| `broken_board_pool` | `date` | 炸板池 |
| `limit_down_pool` | `date` | 跌停池 |
| `yesterday_limit_up_pool` | `date` | 昨日涨停今日表现（晋级率输入） |
| `strong_stock_pool` | `date` | 强势股池 |
| `ths_limit_up_pool` | `date` | 同花顺涨停揭秘：涨停原因、板型、封板质量 |
| `dragon_tiger` | `date`, `code`, `page_size` | 龙虎榜（按日期和/或个股） |
| `dragon_tiger_seats` | `code`, `trade_date`, `look_back` | 龙虎榜席位：买卖前五、机构资金 |
| `dragon_tiger_backup` | `trade_date`, `max_pages` | 交易所官方龙虎榜（上交所定宽 + 深交所 JSON） |
| `industry_boards` | `page`, `page_size` | 行业板块排行 + 主力资金 |
| `concept_boards` | `page`, `page_size` | 概念板块排行 |
| `board_members` | `board_code`, `page`, `page_size` | 行业 / 概念板块成分股（如 `BK0475`） |
| `board_history` | `board_code`, `days` | 板块日 K 线 |
| `stock_concept_blocks` | `code` | 个股所属行业 / 概念 / 地域 |
| `monitor_pool` | `only_active` | 重点监控池及有效窗口 |
| `price_anomaly` | `page_size`, `page_no` | 严重异常波动明细 |
| `price_anomaly_count` | `page_size`, `page_no`, `sort_key`, `sort_dir` | 个股异动次数排行 |
| `stock_changes` | `type_codes`, `page`, `page_size` | 盘口异动：快速涨跌、大笔买卖、封板事件 |
| `st_stocks` | `page`, `page_size` | ST / *ST 名单（东财不可用时回退 baostock） |
| `ths_hot_rank` | `list_type` | 同花顺热榜及短评 |
| `eastmoney_hot_rank` | `page_size` | 东财股吧人气榜（备用热榜） |

</details>

<!-- 资金 -->

<a id="layer-funds"></a>

### 资金层

<details open>
<summary><b>16 个工具</b> — 资金流 / 北向 / 两融 / 大宗 / 股东户数 / 分红 / 筹码 / ETF 份额</summary>

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `realtime_fund_flow` | `code` | 个股主力资金实时快照 |
| `stock_fund_flow` | `code`, `days` | 个股主力 / 大 / 中 / 小 / 超大单净流入 |
| `fund_flow_ranking` | `scope`, `page`, `page_size` | 个股 / 行业 / 概念资金流排行（含 5 / 10 日） |
| `board_fund_flow` | `board_type`, `page`, `page_size` | 板块资金流排行（含 5 / 10 日） |
| `fund_flow_backup` | `code` | 新浪个股实时资金流（备用） |
| `fund_flow_history_backup` | `code`, `days` | 新浪资金流历史（备用） |
| `northbound_flow` | `history`, `days` | 北向 / 南向净流入；`history=True` 为日度历史 |
| `margin_detail` | `code`, `page_size` | 个股融资融券历史 |
| `margin_market` | `days` | 全市场两融余额 |
| `margin_trading_backup` | `trade_date`, `exchange`, `code`, `max_pages` | 交易所官方两融（深交所 T+1，自动回退一个交易日） |
| `block_trades` | `code`, `date`, `page_size` | 大宗交易 |
| `holder_number` | `code` | 股东户数快照 |
| `dividend_history` | `code` | 分红送转历史 |
| `chip_distribution` | `code`, `days`, `grid_size`, `decay` | 筹码分布（本地由 OHLC + 换手率模拟） |
| `etf_quotes` | `page`, `page_size` | ETF 全市场快照 |
| `etf_share_history` | `code`, `page_size` | ETF 份额历史 |

</details>

<!-- 宏观 -->

<a id="layer-macro"></a>

### 宏观层

<details open>
<summary><b>12 个工具</b> — GDP / CPI / PMI / 货币 / 社融 / LPR / 利率 / 日历</summary>

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `china_gdp` | `page_size` | 季度 GDP（累计值，亿元）与同比 |
| `china_cpi` | `page_size` | CPI：同比、环比、累计指数 |
| `china_pmi` | `page_size` | 官方制造业与非制造业 PMI |
| `china_money_supply` | `page_size` | M0 / M1 / M2 与增速 |
| `china_social_financing` | `months` | 社会融资规模（akshare 桥） |
| `china_lpr` | `months` | LPR 历史（akshare 桥） |
| `china_retail_sales` | `page_size` | 社会消费品零售总额 |
| `economy_snapshot` | — | 主要经济与价格指标的最新值，一次取回 |
| `bond_yield_curve` | `date` | 中债国债收益率曲线（akshare 桥） |
| `repo_rate` | `days` | 银行间回购定盘利率（DR007 等） |
| `shibor_rates` | `days` | Shibor 期限结构（货币网，失败回退 akshare） |
| `macro_calendar` | `date` | 经济日历（尽力而为，akshare 桥） |

</details>

<!-- 公告 -->

<a id="layer-announcements"></a>

### 公告层

<details open>
<summary><b>9 个工具</b> — 巨潮 / 深交所 / 风险分类 / 新闻与快讯</summary>

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `cninfo_announcements` | `code`, `start`, `end`, `category`, `limit`, `page` | 巨潮公告（沪 / 深 / 北）含 PDF 链接 |
| `cninfo_announcement_risk` | `code`, `start`, `end`, `limit` | 把近期公告归类为结构化风险事件 |
| `szse_announcements` | `code`, `start`, `end`, `page`, `page_size` | 深交所官方公告（备用源） |
| `announcements_backup` | `code`, `page_size`, `page_index` | 官方公告列表（深交所直连，其余走东财） |
| `cls_telegraph` | `limit` | 财联社 7×24 电报 |
| `wallstreetcn_lives` | `channel`, `limit` | 华尔街见闻 7×24（A 股 / 全球） |
| `eastmoney_global_news` | `limit` | 东财全球快讯 |
| `eastmoney_stock_news` | `code`, `limit` | 个股新闻（行情页信息流） |
| `cctv_news` | `date` | 新闻联播条目 |

</details>

<!-- 指数 -->

<a id="layer-index"></a>

### 指数层

<details open>
<summary><b>10 个工具</b> — 成分 / 权重 / 估值 / K 线 / 日历 / 申万</summary>

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `index_list` | `scope` | 指数列表（`all` / `sh` / `sz` / `star`） |
| `index_kline` | `code`, `days` | 指数日 K（沪 / 深 / 中证 / 科创） |
| `index_constituents` | `index_code` | 成分股列表 |
| `index_weights` | `index_code` | 成分权重（东财成分报表） |
| `index_valuation` | `code` | 指数点位、成交额，以及已发布的 PE / PB |
| `is_trading_day` | `date` | 是否交易日 |
| `trading_calendar` | `start`, `end`, `limit` | 交易日历（由上证指数日 K 推导） |
| `market_calendar_summary` | `days` | 最近交易日 + 今日交易状态 |
| `sw_industry_index` | `code`, `days` | 申万行业指数（多 secid 布局尽力而为） |
| `sw_industry_history` | `code`, `as_of`, `limit` | 个股申万行业分类变迁史 |

</details>

<!-- 期货 -->

<a id="layer-futures"></a>

### 期货层

<details open>
<summary><b>7 个工具</b> — 国内期货 / 持仓 / 期权 / A50 / 上海金</summary>

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `futures_realtime` | `symbols` | 国内期货实时行情（新浪，唯一覆盖大商所） |
| `futures_daily` | `date`, `exchange` | 交易所官方日行情（上期所 / 能源 / 郑商所 / 中金所 / 广期所 / ALL） |
| `futures_kline` | `symbol`, `start`, `end` | 期货日 K（主力连续或具体合约） |
| `futures_position_rank` | `date`, `exchange`, `symbol` | 会员成交量 / 多空持仓前 20 |
| `options_daily` | `date`, `exchange` | 商品与股指期权日行情（交易所官方） |
| `a50_futures` | — | 富时中国 A50 期货（离岸风向标） |
| `sge_spot` | `instrument` | 上海金 / 上海银现货日行情（金：元/克；银：元/千克） |

</details>

<!-- 事件 -->

<a id="layer-event"></a>

### 事件层

<details open>
<summary><b>7 个工具</b> — 业绩预告 / 调研 / 增减持 / 回购 / 质押 / 解禁 / 打新</summary>

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `earnings_forecast` | `code`, `report_date`, `limit` | 业绩预告（全市场或单只） |
| `institution_survey` | `code`, `start`, `end`, `detail`, `limit` | 机构调研（公司 IR 记录聚合） |
| `holder_trades` | `code`, `direction`, `start`, `end`, `limit` | 大股东增减持 |
| `share_buyback` | `code`, `progress`, `limit` | 回购计划与实施进度 |
| `equity_pledge` | `code`, `date`, `limit` | 股权质押（中国结算周度统计） |
| `lockup_expiry` | `code`, `trade_date`, `forward_days` | 限售解禁：历史 + 未来窗口 |
| `ipo_calendar` | `limit` | 新股申购日历（最新在前） |

</details>

<!-- 研报 -->

<a id="layer-research"></a>

### 研报层

<details open>
<summary><b>6 个工具</b> — 个股 / 行业研报 / PDF / 一致预期 / 自然语言搜索</summary>

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `eastmoney_reports` | `code`, `industry_code`, `page`, `page_size` | 券商研报，含评级与 PDF 链接 |
| `eastmoney_industry_reports` | `industry_code`, `page`, `page_size` | 行业研报（东财行业数字 id，如 `447`） |
| `eastmoney_report_pdf` | `info_code` | 解析研报 PDF 下载地址 |
| `ths_eps_forecast` | `code` | 机构一致预期 EPS（分年度、分析师数、评级） |
| `sina_research_reports` | `code`, `page`, `page_size` | 新浪研报列表（备用源） |
| `iwencai_search` | `query`, `limit` | 自然语言搜索（需 `IWENCAI_API_KEY`） |

</details>

<!-- 舆情 -->

<a id="layer-sentiment"></a>

### 舆情互动层

<details open>
<summary><b>3 个工具</b> — 互动易 / 上证 e 互动 / 热门概念</summary>

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `cninfo_irm` | `code`, `page_size`, `page_num` | 巨潮互动易（深市）投资者问答 |
| `sse_e_interaction` | `code`, `kind`, `page`, `page_size` | 上证 e 互动（沪市）问答 |
| `em_hot_concept` | `code` | 市场当前赋予个股的热门概念 |

</details>

<!-- 期权 / 可转债 / 估值 -->

<a id="layer-option"></a>

### 期权 · 可转债 · 估值

<details open>
<summary><b>5 个工具</b></summary>

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `sina_option_codes` | `underlying`, `call` | ETF 期权合约链（按到期月分组） |
| `sina_option_greeks` | `code` | 交易所口径希腊字母、隐含波动率、理论价 |
| `sina_option_tquote` | `code` | 期权 T 型报价：五档、持仓量、行权价与涨跌停 |
| `convertible_bonds` | `include_delisted` | 全市场可转债条款 + 实时转股价值 / 溢价率；可选含已摘牌 |
| `full_valuation` | `code`, `target_pe` | 前瞻 PE、PEG、PE 消化年限（一致预期 EPS + 实时价） |

</details>

<!-- 别名 -->

<a id="layer-aliases"></a>

### 兼容别名（25 个）

旧名字继续可用，`tools/list` 同时返回别名，调用效果与目标工具完全一致。

| 别名 | 指向 | 别名 | 指向 |
| --- | --- | --- | --- |
| `stock_info` | `stock_profile` | `get_kline` | `tencent_kline` |
| `get_finance_report` | `eastmoney_financial_report` | `block_trade` | `block_trades` |
| `margin_trading` | `margin_detail` | `holder_num_change` | `holder_number` |
| `etf_shares` | `etf_share_history` | `hsgt_realtime` | `northbound_flow` |
| `stock_fund_flow_120d` | `stock_fund_flow` | `em_zt_pool` | `limit_up_pool` |
| `em_dt_pool` | `limit_down_pool` | `em_zb_pool` | `broken_board_pool` |
| `em_yzt_pool` | `yesterday_limit_up_pool` | `daily_dragon_tiger` | `dragon_tiger` |
| `dragon_tiger_board` | `dragon_tiger` | `em_hot_rank` | `eastmoney_hot_rank` |
| `ths_hot_list` | `ths_hot_rank` | `limit_up_sentiment` | `market_sentiment` |
| `st_stock_list` | `st_stocks` | `nbs_pmi` | `china_pmi` |
| `lpr_history` | `china_lpr` | `pboc_social_financing` | `china_social_financing` |
| `repo_fixing_rates` | `repo_rate` | `chinabond_yield_curve` | `bond_yield_curve` |
| `iwencai_query` | `iwencai_search` | | |

---

<a id="envelope"></a>

## 返回结构与通用参数

成功响应统一为：

```json
{
  "ok": true,
  "data": [ ... ],
  "meta": {
    "source": "tencent",
    "fetched_at": "2026-10-24T10:21:05+08:00",
    "cache_hit": false,
    "confidence": "A",
    "trace_id": "9ecc979e8192435b"
  }
}
```

失败时带结构化错误（`code` / `message` / `suggestion`），不会用模型猜测填充。

每个工具都接受这些通用参数：

| 参数 | 作用 |
| --- | --- |
| `fields` | 列投影，如 `fields="code,name,price"`；显式传入优先于 tier |
| `tier` | 预设档位：`brief`（身份 + 价格快照）、`medium`（+ OHLC/估值/上下文）、`full`（不裁剪） |
| `data_as_of` | ISO-8601 时点视图：丢弃该时点之后才发布的记录，用于回测与复盘 |
| `_refresh` | 忽略缓存重新抓取并回填 |
| `_no_cache` | 本次不读也不写缓存 |

tier 的默认规则在 `field_map.toml` 的 `[tiers.brief]` / `[tiers.medium]` 里按工具配置，
`"*"` 表示默认值。

---

<a id="examples"></a>

## 使用示例

### 1. 看一眼个股

```bash
th-cn stock info 600000
th-cn tool call tencent_quote --params '{"codes": ["600000", "000001", "300750"]}'
```

```python
from tickhub_cn import StockDataClient
c = StockDataClient()
row = c.tencent_quote("600000")["data"][0]
print(row["name"], row["price"], row["pe"], row["pb"])
```

### 2. K 线与复权

```bash
th-cn kline get 600000 --period daily --adjust qfq --limit 60
th-cn kline batch --codes 600000,000001 --period weekly --limit 30
th-cn tool call tencent_minute --params '{"code":"600000","period":5,"limit":48}'
```

### 3. 财报与勾稽

```bash
th-cn finance report 600000 --granularity income
th-cn tool call eastmoney_financial_all --params '{"code":"600000","periods":8}'
th-cn tool call finance_crosscheck --params '{"code":"600000","strict":true}'
th-cn tool call main_business --params '{"code":"600000","periods":4}'
```

### 4. 当日盘面

```bash
th-cn tool call market_overview
th-cn tool call limit_up_pool --params '{"date":"2026-10-24"}'
th-cn tool call dragon_tiger_seats --params '{"code":"600000","look_back":5}'
th-cn tool call industry_boards --params '{"page":1,"page_size":20}'
```

### 5. 资金与北向

```bash
th-cn tool call realtime_fund_flow --params '{"code":"600000"}'
th-cn tool call stock_fund_flow --params '{"code":"600000","days":20}'
th-cn tool call northbound_flow --params '{"history":true,"days":30}'
th-cn tool call margin_detail --params '{"code":"600000","page_size":10}'
```

### 6. 公告、新闻、研报

```bash
th-cn announcement list 600000 --start 2026-09-25 --end 2026-10-25 --limit 30
th-cn tool call cls_telegraph --params '{"limit":30}'
th-cn research list 600000 --limit 10
th-cn tool call ths_eps_forecast --params '{"code":"600000"}'
th-cn tool call cninfo_financial_notes --params '{"code":"600000","year":2025,"extract":true}'
```

### 7. 时点回看（回测）

```python
from tickhub_cn import StockDataClient
c = StockDataClient()
# 只保留 2025-03-31 当天及之前已经发布的记录
rows = c.call("eastmoney_financial_report", {
    "code": "600000",
    "statement": "income",
    "data_as_of": "2025-03-31T23:59:59+08:00",
})
```

### 8. 批量健康检查

```bash
th-cn doctor
th-cn source health
th-cn cache stats
```

---

<a id="proxy"></a>

## 代理配置（单代理与代理池）

上游数据站对出口 IP 有频次限制，网络环境不稳定时可以通过代理出口。代理是**可选**的，
默认全部关闭；开启后所有出站请求（数据抓取、健康探测）都走代理。

### 单代理

```bash
PROXY_URL=http://user:password@proxy.example.com:8080
```

`PROXY_URL` 支持 `http://` / `https://`，也支持在 URL 中带用户名密码。
`th-cn config show` 默认把口令显示为 `***`。

### 代理池

多个认证代理节点用代理池管理，默认关闭，必须显式开启：

```bash
PROXY_POOL_ENABLE=true

# 方式一：配置文件（推荐，凭证不进入环境变量/命令行历史）
PROXY_POOL_CONFIG_PATH=logs/proxy_pool.json

# 方式二：环境变量，分号分隔（仅适合无认证或凭证可暴露的场景）
PROXY_POOL_LIST="http://host-a:15818;http://host-b:15818"

# 方式三：环境变量，JSON（节点可带认证与请求头）
PROXY_POOL_LIST='[{"url":"http://host-a:15818","username":"USER","password":"PASS"}]'
```

**读取顺序**：`PROXY_POOL_CONFIG_PATH` 指向的文件存在时以文件为准；文件不存在才读
`PROXY_POOL_LIST`。文件损坏会记 `QFSD-3001` 并回退到 `PROXY_POOL_LIST`。

### 节点文件格式

`logs/proxy_pool.json`（默认路径，已被 `.gitignore` 覆盖）：

```json
{
  "proxies": [
    {
      "url": "http://proxy-a.example.com:15818",
      "username": "USER",
      "password": "PASS",
      "headers": {},
      "enabled": true
    },
    {
      "url": "http://proxy-b.example.com:15818",
      "username": "USER",
      "password": "PASS",
      "enabled": true
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `url` | 代理地址；可写 `http://host:port`，也可直接写 `host:port`（自动补 `http://`） |
| `username` / `password` | 认证信息；写在这里比写进 URL 更安全 |
| `headers` | 附加请求头（少数代理需要）；展示时值一律替换为 `***` |
| `enabled` | `false` 表示保留配置但不参与调度 |

### 健康检查与切换

- **轮询**：请求按轮询顺序取节点，不总压同一个出口。
- **三振出局**：连续 3 次连接级失败后标记不健康（`failure_threshold = 3`），
  任意一次成功即清零。
- **重试换出口**：单次请求重试时会跳过刚失败的节点，不会在同一个坏节点上连撞。
- **全挂兜底**：所有节点都不健康时退回直连，并在日志与 `meta` 中记录 `QFSD-5001`，
  产品继续可用，而不是整体失败。
- **后台巡检**：`PROXY_CHECK_INTERVAL` 秒（默认 60）巡检一次，
  `PROXY_CHECK_TIMEOUT` 秒（默认 10）为单节点超时；巡检结果回写健康状态与延迟。

### 命令行与管理台

```bash
th-cn proxy list      # 列出节点：URL 去掉 userinfo，账号显示 t1***，口令/头显示 ***
th-cn proxy check     # 逐个探测，输出 healthy / latency_ms / last_error
```

管理台的「代理池」页与 CLI 等价，并支持：

- 新增 / 删除节点、启用 / 停用单个节点（不需要改文件、不需要重启进程）；
- 一键重载配置文件（`/api/proxy/reload`）；
- 一键巡检（`/api/proxy/check`），表格里直接看延迟和失败次数；
- 页面与 API 返回的节点句柄是 12 位哈希 `key`，**不回传凭证**。

### 安全注意

- 代理凭证只放 `logs/proxy_pool.json`（或环境变量），不要提交进 Git；
  `logs/` 已在 `.gitignore` 中。
- 日志、`th-cn config show`、管理台、`proxy list` 的输出都做脱敏；
  `config show --show-secrets` 是唯一的显式例外，只在本机调试时使用。
- `PROXY_POOL_LIST` 出现在 shell 历史 / 进程环境里，长期使用建议改用配置文件。

---

<a id="cache"></a>

## 缓存与降级

Redis 是**可选**的。连接得上时使用分层 TTL：

| Tier | TTL | 典型数据 |
| --- | --- | --- |
| `realtime` | 60 s | 实时行情、资金流快照 |
| `semi_static` | 24 h | 日 K、龙虎榜、板块、财报 |
| `static` | 7 d | 交易日历、成分、行业分类 |
| `health` | 120 s | 数据源健康探测 |

缓存键：`qf:{env}:cache:{domain}:{code}:{granularity}:{params_hash}`。
Redis 不可用时自动降级为进程内缓存，`doctor` 报 `degraded` 并给 `QFSD-2001`，
**这是设计内的状态，不是故障**。

```bash
th-cn cache stats
th-cn cache refresh --domain kline --code 600000 --tool tencent_kline
th-cn cache refresh --domain quote        # 使整个域失效
th-cn cache flush                         # 清空
th-cn cache flush --prefix kline:600000   # 只清一个切片
```

上游失败时按备用源链切换；切换会写进 `meta`（`source`、`fallback_from`）。
批量调用返回逐条成败，不会因为一条失败丢掉整批。`push2` 熔断期间，
只能走 push2 的工具会自动切到非 push2 替代源，并使用旧缓存兜底。

---

<a id="admin"></a>

## 管理台（Admin Console）

管理台是可选组件（需要 `.[admin]`），默认关闭，只监听回环地址。

```bash
pip install -e ".[admin]"
ADMIN_ENABLE=true ADMIN_AUTH_TOKEN=change-me th-cn admin serve
# http://127.0.0.1:8787
```

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `ADMIN_ENABLE` | `false` | 是否允许启动 |
| `ADMIN_HOST` / `ADMIN_PORT` | `127.0.0.1` / `8787` | 监听地址 |
| `ADMIN_AUTH_TOKEN` | 空 | 设置后，请求需带 `X-Admin-Token` |
| `ADMIN_READONLY` | `false` | 只读模式：屏蔽写操作，但放行只读工具的 MCP 调试调用 |
| `ADMIN_CORS_ORIGINS` | 空 | 允许的跨域来源 |
| `ADMIN_AUDIT_SINK` | `file` | 审计日志落点 |
| `ADMIN_SENSITIVE_MASK` | `true` | 敏感字段脱敏 |

页面（哈希路由，单文件，无外部依赖）：

| 页面 | 内容 |
| --- | --- |
| 概览 | 进程、缓存后端、数据源健康、队列、勾稽状态 |
| 数据源 | 逐个探测并显示延迟与最后错误 |
| 缓存 | 统计、按域/前缀失效 |
| 代理池 | 节点增删启停、巡检、重载（凭证脱敏） |
| 字段映射 | `field_map.toml` 校验与预览 |
| MCP 调试 | 149 个名称搜索、参数表单、直接调用；结果顶部显示降级与 `data_as_of` |
| 数据浏览 | 常用工具的表格视图，支持排序、跨页搜索、xlsx 导出 |
| 爬虫 | 任务状态、手动触发、进度、取消、重试 |
| 北向 | 归档中的北向日度快照面板 |
| 日志 | 结构化日志查询 |
| 配置 | 运行时开关（含 AI 文本清洗开关），密钥脱敏 |
| 审计 | 管理操作审计流水 |

常用 API 与页面一一对应：`/api/status`、`/api/sources`、`/api/cache/*`、
`/api/proxy/*`、`/api/fieldmap`、`/api/mcp/tools|call`、`/api/crawler/*`、
`/api/archive/northbound`、`/api/logs`、`/api/config`、`/api/queue`、
`/api/crosscheck`、`/api/risk`。

---

<a id="crawler"></a>

## 离线爬虫（盘后预热）

爬虫只做**提前归档**，永远不是实时查询的前置条件。归档库默认在
`var/archive/tickhub.db`（SQLite）。

```bash
th-cn crawler status
th-cn crawler run --task all
th-cn crawler run --task announcements --codes 600000,000001 --days 7
th-cn crawler daemon            # 阻塞式定时循环
```

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `OFFLINE_CRAWLER_ENABLE` | `false` | `start-all --crawler auto` 是否拉起子进程 |
| `OFFLINE_CRAWLER_CRON` | `0 2 * * *` | 主任务时间（盘后） |
| `NORTHBOUND_CRON` | `5 15 * * 1-5` | 北向快照时间（收盘后） |
| `OFFLINE_CRAWLER_CONCURRENCY` | — | 并发度 |

任务类型：`announcements` / `reports` / `risks` / `northbound` / `all`。
北向历史就是每日快照累积出来的；归档为空只意味着历史面板少数据，
不影响 `northbound_flow` 的实时查询。

---

<a id="config"></a>

## 配置项

所有配置都来自环境变量（存在 `.env` 时自动加载），每一项都有默认值，
复制 `.env.example` 即可开始。密钥在 `config show` 中默认脱敏。

| 变量 | 默认 | 用途 |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `LOG_SINK` | `stderr` | `stderr` / `file` / `sqlite` / `mysql` / `postgres` |
| `REDIS_URL` | 空 | 不配则用进程内缓存 |
| `PROXY_URL` | 空 | 单代理 |
| `PROXY_POOL_ENABLE` | `false` | 代理池总开关 |
| `PROXY_POOL_CONFIG_PATH` | `logs/proxy_pool.json` | 节点文件 |
| `PROXY_POOL_LIST` | 空 | 分号分隔或 JSON 的节点列表 |
| `PROXY_CHECK_TIMEOUT` / `PROXY_CHECK_INTERVAL` | `10` / `60` | 巡检超时与间隔（秒） |
| `TDX_PATH` | 空 | 通达信安装目录（盘后包工具） |
| `FIELD_MAP_PATH` | `field_map.toml` | 字段别名 / 隐藏 / 默认投影 |
| `CACHE_ENV` | 空 | 缓存键的 `{env}` 段，隔离不同环境 |
| `IWENCAI_API_KEY` | 空 | 开启 `iwencai_search` |
| `TUSHARE_TOKEN` | 空 | 开启 Tushare 可选源 |
| `ENABLE_AKSHARE` / `ENABLE_MOOTDX` / `ENABLE_IWENCAI` / `ENABLE_TUSHARE` | `false` | 可选源开关 |
| `SZSE_ALLOW_HTTP` | `false` | 深交所 HTTPS 失败时是否回退 HTTP |
| `OFFLINE_CRAWLER_ENABLE` | `false` | 爬虫开关 |
| `ADMIN_ENABLE` | `false` | 管理台开关 |
| `ADMIN_AUTH_TOKEN` | 空 | 管理台令牌 |
| `CRAWL4AI_ENABLE` | `false` | AI 长文本清洗（只清洗公告/研报文字，**从不生成数字**） |

配置文件（全部可选，缺失即使用内置默认值）：

| 文件 | 用途 |
| --- | --- |
| `.env` | 运行时设置 |
| `field_map.toml` | 外部字段别名、隐藏字段、默认投影、tier 覆盖 |
| `logs/proxy_pool.json` | 认证代理节点（`PROXY_POOL_ENABLE=true` 时生效，git 忽略） |
| `model_gateway.toml` | 可选多模型网关（默认关闭，只用于文本清洗） |
| `config/admin.toml` | 管理台设置 |

格式错误的配置文件会报 `QFSD-3001` 并使用安全回退，不会让进程起不来。

---

<a id="errors"></a>

## 错误码

每个错误都带稳定代码、消息与建议动作。`QFSD-xxxx` 为标准写法，
`QF-xxxx` 别名仍然接受。

| 代码 | 含义 | CLI 退出码 |
| --- | --- | --- |
| `QFSD-1001` / `1002` | 数据源超时 / 返回为空 | 4 |
| `QFSD-2001` | Redis 不可用（已降级） | 5 |
| `QFSD-3001` | 字段映射配置无效 | 3 |
| `QFSD-4001` | 财务勾稽校验失败 | 1 |
| `QFSD-5001` | 代理池全部不可用（已直连兜底） | 1 |
| `QFSD-6001` | 管理台鉴权失败 | 6 |
| `QFSD-7001` | 触发限流 | 1 |
| `QFSD-8001` | 批量调用部分失败 | 8 |
| `QFSD-9001` | 参数非法 | 2 |
| `QFSD-9002` | 未知工具名 / 不支持 | 1 |
| `QFSD-9003` | 数据源错误 | 4 |
| `QFSD-9004` | 需要鉴权（可选源未配置 key） | 6 |
| `QFSD-9005` | 无数据 | 1 |

> `QFSD-2001` 与 `QFSD-5001` 是**降级提示**，不是调用失败：
> 前者表示缓存改成进程内，后者表示代理不可用改走直连。

---

<a id="faq"></a>

## FAQ

**需要 API Key 或账号吗？**
不需要。核心数据源（腾讯、交易所、巨潮、新浪、东财等）都是公开接口，无需登录。
只有 `iwencai_search`、Tushare 等可选源需要各自密钥，并且默认关闭。

**一定要装 Redis 吗？**
不用。没有 Redis 就用进程内缓存，`doctor` 显示 `degraded` 是正常设计。

**为什么某个工具返回 `QFSD-1001/1002/9005`？**
上游超时、休市或暂时无数据。先跑 `th-cn source health` 看是哪个源的问题；
不要循环重试，限流状态下重试只会让 IP 更难受。数据源恢复后用
`th-cn cache refresh --domain ...` 让下一次查询走实时。

**`date` 参数怎么传？**
统一用 `YYYY-MM-DD`。省略时由工具选择默认日期（通常是最近交易日）；
需要可复现的结果时请显式传入。

**浏览器能直接调 `/mcp` 吗？**
不能。Streamable HTTP 是 JSON-RPC，不是 REST。浏览器端需要 MCP SDK，
或使用仓库自带的 `web/` 网关（只代理少量只读工具）。

**能保证回测不使用未来数据吗？**
可以使用 `data_as_of`：它会在返回前丢弃该时点之后才发布的记录。
但归档与缓存仍以抓取时刻为准，严格回测请显式固定 `data_as_of`。

**为什么代码里导入 `tickhub_cn` 没有启动任何东西？**
这是刻意的：导入没有副作用、不开 socket。服务只在显式入口
（`th-cn mcp stdio|http`、`start-all`、`admin serve`）才会启动。

**代理开了但请求还是直连？**
确认 `PROXY_POOL_ENABLE=true`，然后 `th-cn proxy list` 能看到节点，
`th-cn proxy check` 显示 `healthy`。全部不健康时会按设计直连并记 `QFSD-5001`。

**南北向、涨停池这些数据能拿去实盘决策吗？**
数据来自公开上游，只做归一化与来源标注，不构成投资建议；上游口径以交易所公告为准。

---

<a id="verify"></a>

## 验证

```bash
th-cn version
th-cn doctor                      # 环境 / 缓存 / 配置 / 数据源
th-cn source health               # 逐源探测与延迟
th-cn tool list --output json     # 149 个 MCP 名称
th-cn proxy list                  # 代理节点（凭证脱敏）
th-cn fieldmap validate
th-cn config check
```

MCP 冒烟（协议帧层面，仓库内已有测试）：

```bash
th-cn mcp stdio                   # initialize → tools/list → tools/call
th-cn start-all --port 8000 --crawler off
# 客户端连 http://127.0.0.1:8000/mcp
```

离线测试（不联网）：

```bash
pip install -e ".[dev]"
pytest
```

联网测试（会访问真实上游）：

```bash
pytest -m live
```

---

<a id="docs"></a>

## 文档

- [docs/CLI_USAGE.md](docs/CLI_USAGE.md) —— 每条命令、参数与退出码
- [docs/MCP_API_CONTRACT.md](docs/MCP_API_CONTRACT.md) —— 工具契约、envelope、别名
- [docs/DATA_MODEL.md](docs/DATA_MODEL.md) —— 实体、字段、单位、缓存键
- [docs/TEST_PLAN.md](docs/TEST_PLAN.md) —— 契约 / 集成 / 回归 / UI 测试
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) —— pip、Docker、systemd、反向代理
- [docs/PIP_DISTRIBUTION.md](docs/PIP_DISTRIBUTION.md) —— 打包与发布
- [docs/MULTI_MODEL_INTEGRATION.md](docs/MULTI_MODEL_INTEGRATION.md) —— 多模型接线与客户端矩阵
- [skills/tickhub-cn/SKILL.md](skills/tickhub-cn/SKILL.md) —— Agent 技能包
- [docs/decisions/](docs/decisions/) —— 架构决策记录（ADR）索引，含历史工单死链说明

---

<a id="dev"></a>

## 开发

```bash
pip install -e ".[dev]"

pytest                          # 离线：单元 + 契约
pytest -m live                  # 联网：真实上游

python scripts/gen_tool_catalog.py --format skill \
  --out skills/tickhub-cn/references/tools.md
```

约定：

- Python ≥ 3.11；`stdout` 只输出数据，日志一律走 `stderr`。
- 新增工具写进 `src/tickhub_cn/datasources/base.py` 的注册表，描述与参数由
  `scripts/gen_tool_catalog.py` 生成引用表，CI 会比对。
- 每个数字都必须可追溯到一次上游响应；禁止在工具里做估算或模型推断。
- 改动数据源行为时，同步 `docs/MCP_API_CONTRACT.md` 与 `docs/DATA_MODEL.md`。

---

<a id="disclaimer"></a>

## 免责声明

本项目是独立实现，不是任何其他行情项目的分支，也不内嵌其代码。
所有数据来自各公开数据源，版权与使用条款归各数据源所有；本项目只做抓取、
归一化与缓存，不对数据的完整性、及时性作担保，不构成任何投资建议。
请遵守上游站点的使用条款，控制请求频率。

## License

Apache-2.0。见 [LICENSE](LICENSE)。
