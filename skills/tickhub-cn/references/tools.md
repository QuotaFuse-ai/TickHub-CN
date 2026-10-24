# Tool catalogue

124 canonical tools + 25 aliases = 149 MCP names. Generated with `scripts/gen_tool_catalog.py`; do not edit by hand.

TTL is the cache lifetime in seconds; `—` means the alias shares its target's behaviour and the response is not cached separately.

## Quotes & K-lines (`quotes`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `apply_adjust` | `code`, `closes`, `factors` | — | Apply forward-adjustment factors to raw closes (rule-based, no inference). |
| `baidu_kline_with_ma` | `code`, `period`, `limit` | 86400 | K-line with moving averages from Baidu (secondary source). |
| `bse_quote_backup` | `trade_date`, `code`, `max_pages` | 86400 | Beijing Stock Exchange closing snapshot with 5-level book (official source). |
| `etf_quote` | `codes` | 60 | Realtime ETF quote (price, volume, turnover, premium inputs). |
| `index_quote` | `codes` | 60 | Realtime index quote (SH/SZ/BSE indices). |
| `sina_adjust_factor` | `code` | 604800 | Forward-adjustment factor history from Sina. |
| `tdx_daily_package` | `date`, `market`, `download_dir` | 86400 | Whole-market daily bars for one trading day from the TDX after-hours package. |
| `tencent_kline` | `code`, `period`, `adjust`, `limit`, `start`, `end` | 86400 | Daily/weekly/monthly K-line with forward/backward adjustment (Tencent). |
| `tencent_market_snapshot` | `codes` | 60 | Snapshot for indices/ETFs/stocks in one call. |
| `tencent_minute` | `code`, `period`, `limit` | 60 | Intraday minute K-line (1/5/15/30/60 minutes) from Tencent. |
| `tencent_quote` | `codes` | 60 | Realtime A-share quote from Tencent (price, PE, PB, turnover, caps). |
| `tencent_quote_batch` | `codes`, `max_codes` | 60 | Batch realtime quotes with partial-failure reporting. |
| `tencent_ticks` | `code`, `page` | 60 | Tick-by-tick trades for the latest session (Tencent, no BSE). |

## Financials (`financials`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `baostock_st_list` | — | 604800 | Historical ST / *ST records (baostock). |
| `baostock_stock_basic` | `code` | 604800 | Listing / delisting dates and status (baostock). |
| `baostock_valuation_history` | `code`, `start`, `end` | 604800 | Historical PE/PB/PS/PCF and turnover (baostock, no BSE). |
| `cninfo_financial_notes` | `code`, `year`, `limit` | 86400 | Locate annual and audit reports that carry the financial-statement notes (CNINFO PDFs). |
| `eastmoney_finance_indicators` | `code`, `periods` | 86400 | Key financial indicators (EPS, BPS, revenue, net profit, ROE). |
| `eastmoney_financial_all` | `code`, `periods` | 86400 | All three statements plus key indicators for the latest periods. |
| `eastmoney_financial_report` | `code`, `statement`, `periods`, `report_date` | 86400 | Financial statements (balance / income / cashflow) from Eastmoney datacenter. |
| `finance_crosscheck` | `code`, `report_date`, `tolerance`, `strict` | 86400 | Cross-check financial statements (assets = liabilities + equity, etc.). |
| `main_business` | `code`, `periods` | 86400 | Main business breakdown by industry, product and region (Eastmoney F10). |
| `sina_financial_report` | `code`, `statement`, `periods` | 86400 | Financial statements from Sina (secondary source). |
| `stock_f10_tips` | `code` | 86400 | TDX F10 latest-tips dossier: indicators, announcements, news, block trades and margin (mootdx). |
| `stock_profile` | `code` | 604800 | Stock profile: name, industry, market cap, float shares, PE/PB. |
| `top_shareholders` | `code`, `holder_type`, `periods` | 86400 | Top-10 shareholders (total or free-float) with position changes (Eastmoney F10). |

## Market breadth & pools (`market`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `board_history` | `board_code`, `days` | 60 | Daily K-line for an Eastmoney industry/concept board. |
| `board_members` | `board_code`, `page`, `page_size` | 60 | Constituents of an Eastmoney industry/concept board (e.g. BK0475). |
| `broken_board_pool` | `date` | 60 | Broken limit-up pool (炸板) for a trading date. |
| `concept_boards` | `page`, `page_size` | 60 | Concept board ranking (东方财富概念板块). |
| `dragon_tiger` | `date`, `code`, `page_size` | 60 | Dragon-tiger list (龙虎榜) for a date and/or a single stock. |
| `dragon_tiger_backup` | `trade_date`, `max_pages` | 86400 | Exchange-official 龙虎榜 list for one trading day (SSE fixed-width + SZSE JSON). |
| `dragon_tiger_seats` | `code`, `trade_date`, `look_back` | 86400 | Dragon-tiger seats (席位) for one stock: records, buy/sell TOP5 and institution flow. |
| `eastmoney_hot_rank` | `page_size` | 60 | Fallback heat ranking from Eastmoney's 股吧 popularity board. |
| `industry_boards` | `page`, `page_size` | 60 | Industry board ranking (东方财富行业板块) with main capital flow. |
| `limit_down_pool` | `date` | 60 | Limit-down pool (跌停板) for a trading date. |
| `limit_up_pool` | `date` | 60 | Limit-up pool (涨停板) for a trading date, including streak and seal time. |
| `market_overview` | — | 60 | Headline index levels plus up/down/flat breadth for the Shanghai composite. |
| `market_sentiment` | `date` | 60 | Short-term sentiment gauges derived from limit-up / broken / limit-down pools. |
| `monitor_pool` | `only_active` | 60 | Eastmoney key-monitoring list (重点监控) with its validity window. |
| `price_anomaly` | `page_size`, `page_no` | 60 | Abnormal-move detail (严重异常波动) for the current session. |
| `price_anomaly_count` | `page_size`, `page_no`, `sort_key`, `sort_dir` | 60 | Abnormal-move tally per stock (异动次数 + 现价). |
| `st_stocks` | `page`, `page_size` | 60 | Risk-warning (ST / *ST) board list; falls back to baostock when Eastmoney is down. |
| `stock_changes` | `type_codes`, `page`, `page_size` | 60 | Intraday tick anomalies (盘口异动): fast moves, block buys/sells, seal events. |
| `stock_concept_blocks` | `code` | 60 | Sector / theme membership of one stock (industry, concept and region mixed). |
| `strong_stock_pool` | `date` | 60 | Strong-stock pool (强势股池) for a trading date. |
| `ths_hot_rank` | `list_type` | 60 | 同花顺 hot list: heat ranking with the vendor's short commentary. |
| `ths_limit_up_pool` | `date` | 60 | THS limit-up reveal (涨停揭秘): limit reason, board type and seal quality. |
| `yesterday_limit_up_pool` | `date` | 60 | Yesterday's limit-up pool with today's follow-through (晋级率 input). |

## Funds & flows (`funds`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `block_trades` | `code`, `date`, `page_size` | 86400 | Block-trade (大宗交易) records by stock or trading date. |
| `board_fund_flow` | `board_type`, `page`, `page_size` | 60 | Industry/concept board fund-flow ranking. |
| `chip_distribution` | `code`, `days`, `grid_size`, `decay` | 60 | Chip distribution (筹码分布) simulated locally from OHLC + turnover. |
| `dividend_history` | `code` | 86400 | Dividend / bonus-share history (分红送转) for one stock. |
| `etf_quotes` | `page`, `page_size` | 86400 | ETF market snapshot (price, turnover, market cap). |
| `etf_share_history` | `code`, `page_size` | 86400 | ETF share-scale history parsed from Eastmoney fund archives. |
| `fund_flow_backup` | `code` | 60 | Sina backup for one stock's realtime money flow. |
| `fund_flow_history_backup` | `code`, `days` | 86400 | Sina daily capital-flow history used when Eastmoney flow history is down. |
| `fund_flow_ranking` | `scope`, `page`, `page_size` | 60 | Realtime capital-flow ranking for stocks, industries or concepts. |
| `holder_number` | `code` | 86400 | Latest shareholder-count snapshot (股东户数) for one stock. |
| `margin_detail` | `code`, `page_size` | 86400 | Per-stock margin financing (融资融券) history. |
| `margin_market` | `days` | 86400 | Market-wide margin balance history. |
| `margin_trading_backup` | `trade_date`, `exchange`, `code`, `max_pages` | 86400 | Exchange-official margin trading table (SSE / SZSE), normalised to 元 and 股; SZSE publishes T+1, so an implicit date backs off one trading day. |
| `northbound_flow` | — | 60 | Northbound / southbound intraday net flow (empty outside the session). |
| `realtime_fund_flow` | `code` | 60 | Realtime snapshot of one stock's main-capital flow. |
| `stock_fund_flow` | `code`, `days` | 60 | Daily main/small/medium/large/super-large net inflow for one stock. |

## Macro & rates (`macro`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `bond_yield_curve` | `date` | 604800 | ChinaBond government-bond yield curve via the optional akshare bridge. |
| `china_cpi` | `page_size` | 604800 | China CPI: year-on-year, month-on-month and cumulative indices. |
| `china_gdp` | `page_size` | 604800 | China GDP by quarter (累计值, 亿元) with year-on-year growth. |
| `china_lpr` | `months` | 604800 | Loan prime rate (LPR) history via the optional akshare bridge. |
| `china_money_supply` | `page_size` | 604800 | China money supply: M0, M1 and M2 (亿元) with growth rates. |
| `china_pmi` | `page_size` | 604800 | China official manufacturing and non-manufacturing PMI. |
| `china_retail_sales` | `page_size` | 604800 | China retail sales of consumer goods. |
| `china_social_financing` | `months` | 604800 | Total social financing (社会融资规模) via the optional akshare bridge. |
| `economy_snapshot` | — | 604800 | Latest print of the main activity and price series in one call. |
| `macro_calendar` | `date` | 604800 | Economic calendar for a date (best effort via the optional akshare bridge). |
| `repo_rate` | `days` | 604800 | Interbank repo fixing (DR007 and peers) via the optional akshare bridge. |
| `shibor_rates` | `days` | 604800 | Shibor term structure (best effort: ChinaMoney page, then akshare). |

## Announcements & news (`announcements`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `announcements_backup` | `code`, `page_size`, `page_index` | 86400 | Official-exchange announcement list with PDF links (SZSE direct, others via Eastmoney). |
| `cctv_news` | `date` | 86400 | CCTV Xinwen Lianbo item list for one date. |
| `cls_telegraph` | `limit` | 60 | Cailianshe (CLS) 7x24 telegraph flashes. |
| `cninfo_announcement_risk` | `code`, `start`, `end`, `limit` | 86400 | Classify recent announcements into structured risk events. |
| `cninfo_announcements` | `code`, `start`, `end`, `category`, `limit`, `page` | 86400 | CNINFO announcements with PDF links (Shanghai/Shenzhen). |
| `eastmoney_global_news` | `limit` | 60 | Eastmoney 7x24 global fast news. |
| `eastmoney_stock_news` | `code`, `limit` | 60 | Individual stock news from the Eastmoney quote-page feed. |
| `szse_announcements` | `code`, `start`, `end`, `page`, `page_size` | 86400 | Shenzhen Stock Exchange official announcements (backup source). |
| `wallstreetcn_lives` | `channel`, `limit` | 60 | Wallstreetcn 7x24 live feed (A-share / global channels). |

## Indices & calendar (`index`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `index_constituents` | `index_code` | 86400 | Constituent list for a supported mainland index (Eastmoney datacenter). |
| `index_kline` | `code`, `days` | 60 | Daily K-line for a mainland index (SSE/SZSE/CSI/STAR). |
| `index_list` | `scope` | 86400 | List mainland indices by market (all / sh / sz / star). |
| `index_valuation` | `code` | 60 | Index level, turnover and (when published) PE/PB multiples. |
| `index_weights` | `index_code` | 86400 | Index constituent weights from the Eastmoney component report. |
| `is_trading_day` | `date` | 604800 | Whether a date is an exchange trading day. |
| `market_calendar_summary` | `days` | 604800 | Recent trading days plus today's session state. |
| `sw_industry_history` | `code`, `as_of`, `limit` | 604800 | Shenwan industry classification history for one stock (SW Research workbook). |
| `sw_industry_index` | `code`, `days` | 86400 | Shenwan industry index history (best effort across Eastmoney secid layouts). |
| `trading_calendar` | `start`, `end`, `limit` | 604800 | Trading dates derived from the SSE composite daily K-line. |

## Futures & commodities (`futures`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `a50_futures` | — | 60 | FTSE China A50 index futures (Sina ``hf_CHA50CFD``) — offshore A-share gauge. |
| `futures_daily` | `date`, `exchange` | 604800 | Futures daily close from the exchange itself (SHFE/INE/CZCE/CFFEX/GFEX, or ALL). |
| `futures_kline` | `symbol`, `start`, `end` | 86400 | Daily futures K-line from Sina — one contract or a main continuous. |
| `futures_position_rank` | `date`, `exchange`, `symbol` | 604800 | Top-20 member volume / long / short positions from the exchange. |
| `futures_realtime` | `symbols` | 60 | Domestic futures realtime quote (Sina) — the only source covering DCE. |
| `options_daily` | `date`, `exchange` | 604800 | Commodity / index option daily close from the exchange itself. |
| `sge_spot` | `instrument` | 86400 | SGE spot daily bars (official) — gold in CNY/g, silver in CNY/kg. |

## Event driven (`event`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `earnings_forecast` | `code`, `report_date`, `limit` | 86400 | Earnings previews (业绩预告) for the whole market or one stock. |
| `equity_pledge` | `code`, `date`, `limit` | 86400 | Equity pledge ratio (股权质押), ChinaClear weekly statistics via Eastmoney. |
| `holder_trades` | `code`, `direction`, `start`, `end`, `limit` | 86400 | Major-shareholder increases/decreases (股东增减持). |
| `institution_survey` | `code`, `start`, `end`, `detail`, `limit` | 86400 | Institution surveys (机构调研) aggregated from company IR records. |
| `ipo_calendar` | `limit` | 86400 | New-share subscription calendar (新股申购日历), newest subscription first. |
| `lockup_expiry` | `code`, `trade_date`, `forward_days` | 86400 | Restricted-share unlock calendar (限售解禁): history plus the coming window. |
| `share_buyback` | `code`, `progress`, `limit` | 86400 | Share buyback plans and their implementation progress (股票回购). |

## Research (`research`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `eastmoney_industry_reports` | `industry_code`, `page`, `page_size` | 86400 | Industry research reports by Eastmoney industry id (numeric, e.g. 447). |
| `eastmoney_report_pdf` | `info_code` | 604800 | Resolve the PDF download URL for an Eastmoney report. |
| `eastmoney_reports` | `code`, `industry_code`, `page`, `page_size` | 86400 | Broker research reports with rating and PDF links (Eastmoney). |
| `iwencai_search` | `query`, `limit` | — | Natural-language research search via iwencai (requires API key). |
| `sina_research_reports` | `code`, `page`, `page_size` | 86400 | Research report list from Sina (secondary source for Eastmoney outages). |
| `ths_eps_forecast` | `code` | 86400 | Institutional consensus EPS by year, with analyst counts and ratings (THS). |

## Sentiment & IR (`sentiment`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `cninfo_irm` | `code`, `page_size`, `page_num` | 60 | Investor questions and official replies from CNINFO 互动易 (Shenzhen listed). |
| `em_hot_concept` | `code` | 60 | Hot concepts the market currently associates with one stock (Eastmoney). |
| `sse_e_interaction` | `code`, `kind`, `page`, `page_size` | 60 | SSE e-Interaction: investor questions and Shanghai-listed company replies. |

## Options (`option`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `sina_option_codes` | `underlying`, `call` | 60 | ETF option contract chain grouped by expiry month (Sina). |
| `sina_option_greeks` | `code` | 60 | Exchange-computed Greeks, implied volatility and theoretical value (Sina). |
| `sina_option_tquote` | `code` | 60 | Option T-quote: bid/ask five levels, open interest, strike and limits (Sina). |

## Convertible bonds (`bond`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `convertible_bonds` | `include_delisted` | 86400 | Whole-market convertible bond table (可转债): terms plus live conversion data. |

## Valuation (`valuation`)

| Tool | Parameters | TTL (s) | Description |
| --- | --- | --- | --- |
| `full_valuation` | `code`, `target_pe` | 60 | Forward PE / PEG / PE-digestion from consensus EPS plus live quote. |

## Compatibility aliases (`alias`)

Aliases mirror the names used by the original a-stock-data client. They take the same parameters as their target.

| Alias | Target | Parameters | Description |
| --- | --- | --- | --- |
| `block_trade` | `block_trades` | `code`, `date`, `page_size` | alias |
| `chinabond_yield_curve` | `bond_yield_curve` | `date` | alias |
| `daily_dragon_tiger` | `dragon_tiger` | `date`, `code`, `page_size` | alias |
| `dragon_tiger_board` | `dragon_tiger` | `date`, `code`, `page_size` | alias |
| `em_dt_pool` | `limit_down_pool` | `date` | alias |
| `em_hot_rank` | `eastmoney_hot_rank` | `page_size` | alias |
| `em_yzt_pool` | `yesterday_limit_up_pool` | `date` | alias |
| `em_zb_pool` | `broken_board_pool` | `date` | alias |
| `em_zt_pool` | `limit_up_pool` | `date` | alias |
| `etf_shares` | `etf_share_history` | `code`, `page_size` | alias |
| `get_finance_report` | `eastmoney_financial_report` | `code`, `statement`, `periods`, `report_date` | alias |
| `get_kline` | `tencent_kline` | `code`, `period`, `adjust`, `limit`, `start`, `end` | alias |
| `holder_num_change` | `holder_number` | `code` | alias |
| `hsgt_realtime` | `northbound_flow` | — | alias |
| `iwencai_query` | `iwencai_search` | `query`, `limit` | alias |
| `limit_up_sentiment` | `market_sentiment` | `date` | alias |
| `lpr_history` | `china_lpr` | `months` | alias |
| `margin_trading` | `margin_detail` | `code`, `page_size` | alias |
| `nbs_pmi` | `china_pmi` | `page_size` | alias |
| `pboc_social_financing` | `china_social_financing` | `months` | alias |
| `repo_fixing_rates` | `repo_rate` | `days` | alias |
| `st_stock_list` | `st_stocks` | `page`, `page_size` | alias |
| `stock_fund_flow_120d` | `stock_fund_flow` | `code`, `days` | alias |
| `stock_info` | `stock_profile` | `code` | alias |
| `ths_hot_list` | `ths_hot_rank` | `list_type` | alias |
