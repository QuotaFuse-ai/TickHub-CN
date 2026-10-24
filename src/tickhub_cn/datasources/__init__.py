"""Data-source layer. Importing this package registers every MCP tool."""

from . import (  # noqa: F401
    announcements,
    backups,
    bonds,
    events,
    f10,
    financials,
    funds,
    futures,
    indices,
    irm,
    macro,
    market,
    options,
    quotes,
    research,
    sw_industry,
    valuation,
)
from .base import (  # noqa: F401
    ALIASES,
    LAYERS,
    REGISTRY,
    call_tool,
    register_alias,
    resolve,
    run_batch,
    tool,
    tool_catalog,
    tool_names,
)

# Compatibility names from the a-stock-data interface. They resolve to the
# equivalent TickHub-CN tool so an existing agent keeps working unchanged.
# This block must run after every module above, because ``register_alias``
# silently skips a target that is not registered yet.
for _alias, _target in {
    "block_trade": "block_trades",
    "chinabond_yield_curve": "bond_yield_curve",
    "daily_dragon_tiger": "dragon_tiger",
    "dragon_tiger_board": "dragon_tiger",
    "em_dt_pool": "limit_down_pool",
    "em_hot_rank": "eastmoney_hot_rank",
    "em_zb_pool": "broken_board_pool",
    "em_zt_pool": "limit_up_pool",
    "em_yzt_pool": "yesterday_limit_up_pool",
    "etf_shares": "etf_share_history",
    "get_finance_report": "eastmoney_financial_report",
    "get_kline": "tencent_kline",
    "holder_num_change": "holder_number",
    "hsgt_realtime": "northbound_flow",
    "iwencai_query": "iwencai_search",
    "limit_up_sentiment": "market_sentiment",
    "lpr_history": "china_lpr",
    "margin_trading": "margin_detail",
    "nbs_pmi": "china_pmi",
    "pboc_social_financing": "china_social_financing",
    "repo_fixing_rates": "repo_rate",
    "st_stock_list": "st_stocks",
    "stock_fund_flow_120d": "stock_fund_flow",
    "stock_info": "stock_profile",
    "ths_hot_list": "ths_hot_rank",
}.items():
    register_alias(_alias, _target)
del _alias, _target

__all__ = [
    "ALIASES",
    "LAYERS",
    "REGISTRY",
    "call_tool",
    "register_alias",
    "resolve",
    "run_batch",
    "tool",
    "tool_catalog",
    "tool_names",
]
