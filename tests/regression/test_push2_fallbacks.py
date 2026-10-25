"""push2 quote paths fall back to non-push2 sources instead of failing.

The upstream blocks a source IP for minutes once it bursts one quote path, so
capital-flow and board tools must keep answering from Sina. Each case pins the
substitute payload shape that was verified live.
"""

from __future__ import annotations

import pytest

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import funds, indices, market, quotes


def _blocked(*_args: object, **_kwargs: object) -> object:
    raise TickHubError(
        "QFSD-7001",
        "push2.eastmoney.com/api/qt/clist/get 连接被重置，熔断中",
        retryable=True,
    )


SINA_HISTORY = [
    {
        "opendate": "2026-10-24",
        "trade": "10.50",
        "changeratio": "0.0123",
        "turnover": "0.008",
        "netamount": "12345678.0",
        "ratioamount": "0.0456",
        "r0_net": "8765432.0",
        "r0_ratio": "0.0321",
        "r0x_ratio": "-0.0010",
        "cnt_r0x_ratio": "0.12",
    }
]


SINA_BOARD_FLOW = [
    {
        "category": "new_jrhy",
        "name": "金融服务",
        "avg_price": "8.21",
        "avg_changeratio": "0.0154",
        "netamount": "987654321.0",
        "ratioamount": "0.0302",
        "inamount": "3000000000.0",
        "outamount": "2012345679.0",
        "ts_symbol": "sh600000",
        "ts_name": "浦发银行",
    }
]


SINA_REALTIME_TEXT = (
    '[{"opendate":"2026-10-24","trade":"10.50","changeratio":"0.0123",'
    '"netamount":"12345678","r0_in":"9000","r0_out":"4000",'
    '"r1_in":"8000","r1_out":"3000","r2_in":"7000","r2_out":"2000",'
    '"r3_in":"6000","r3_out":"1000","name":"浦发银行"}]'
)


SINA_ROSTER = (
    "var S_Finance_bankuai_sinaindustry = {"
    '"new_jrhy":"new_jrhy,金融服务,50,8.21,0.32,1.54,0.88,120000,980000,sh600000,0.51,10.50,浦发银行",'
    '"new_dzxx":"new_dzxx,电子信息,120,15.40,-1.20,-0.75,2.10,340000,560000,sz000001,-0.30,12.10,平安银行"'
    "}"
)


# --------------------------------------------------------------------------- #
# Stock-level capital flow
# --------------------------------------------------------------------------- #


def test_stock_fund_flow_falls_back_to_sina_history(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_request(source: str, method: str, url: str, **kwargs: object) -> object:
        seen.update({"source": source, "url": url, **(kwargs.get("params") or {})})
        return SINA_HISTORY

    monkeypatch.setattr(funds, "em_get", _blocked)
    monkeypatch.setattr(funds, "request", fake_request)

    out = funds.stock_fund_flow("600000", days=5)

    assert out["meta"]["source"] == "sina"
    assert out["meta"]["fallback_from"] == "eastmoney"
    assert "中小单" in out["meta"]["note"]
    assert seen["daima"] == "sh600000"  # type: ignore[index]
    flow = out["data"]["flows"][0]
    assert flow["date"] == "2026-10-24"
    assert flow["main_net"] == 12345678.0
    assert flow["main_pct"] == 4.56
    assert flow["super_large_pct"] == 3.21
    assert flow["change_pct"] == 1.23
    assert flow["small_net"] is None and flow["large_net"] is None


def test_stock_fund_flow_falls_back_when_eastmoney_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(funds, "em_get", lambda *_a, **_k: {"data": {"klines": []}})
    monkeypatch.setattr(funds, "request", lambda *_a, **_k: SINA_HISTORY)

    out = funds.stock_fund_flow("600000")

    assert out["meta"]["source"] == "sina"
    assert out["meta"]["count"] == 1


def test_realtime_fund_flow_falls_back_to_sina(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(funds, "em_get", _blocked)
    monkeypatch.setattr(funds, "request", lambda *_a, **_k: SINA_REALTIME_TEXT)

    out = funds.realtime_fund_flow("600000")

    data = out["data"]
    assert out["meta"]["source"] == "sina"
    assert out["meta"]["fallback_from"] == "eastmoney"
    assert data["main_net"] == 12345678.0
    assert data["super_large_net"] == 5000.0
    assert data["large_net"] == 5000.0
    assert data["medium_net"] == 5000.0
    assert data["small_net"] == 5000.0
    assert data["change_pct"] == 1.23
    assert data["main_net_pct"] is None


# --------------------------------------------------------------------------- #
# Board-level capital flow
# --------------------------------------------------------------------------- #


def test_board_fund_flow_falls_back_to_sina(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_request(_source: str, _method: str, _url: str, **kwargs: object) -> object:
        seen.update(kwargs.get("params") or {})
        return SINA_BOARD_FLOW

    monkeypatch.setattr(funds, "em_get", _blocked)
    monkeypatch.setattr(funds, "request", fake_request)

    out = funds.board_fund_flow("industry", page=1, page_size=20)

    assert seen["fenlei"] == 0
    assert out["meta"]["source"] == "sina"
    assert out["meta"]["fallback_from"] == "eastmoney"
    assert out["data"]["total"] is None
    item = out["data"]["items"][0]
    assert item["code"] == "new_jrhy"
    assert item["name"] == "金融服务"
    assert item["main_net"] == 987654321.0
    assert item["main_net_pct"] == 3.02
    assert item["change_pct"] == 1.54
    assert item["inflow"] == 3000000000.0
    assert item["leader_code"] == "sh600000"
    assert item["super_large_net"] is None


def test_concept_flow_asks_sina_for_concept_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_request(_source: str, _method: str, _url: str, **kwargs: object) -> object:
        seen.update(kwargs.get("params") or {})
        return SINA_BOARD_FLOW

    monkeypatch.setattr(funds, "em_get", _blocked)
    monkeypatch.setattr(funds, "request", fake_request)

    out = funds.board_fund_flow("concept")

    assert seen["fenlei"] == 1
    assert out["data"]["scope"] == "concept"


DATACENTER_ROW = {
    "SECURITY_CODE": "000001",
    "SECURITY_NAME_ABBR": "平安银行",
    "TRADE_DATE": "2026-10-23 00:00:00",
    "CLOSE_PRICE": 11.2,
    "CHANGE_RATE": -0.885,
    "PRIME_INFLOW": -167603481,
    "SUPERDEAL_INFLOW": 202485751,
    "SUPERDEAL_OUTFLOW": 332511136,
    "BIGDEAL_INFLOW": 399074848,
    "BIGDEAL_OUTFLOW": 436652944,
    "BUY_SUPERDEAL_RATIO": 0.1244,
    "BUY_BIGDEAL_RATIO": 0.2451,
}


def _fund_flow_em_get(seen: dict[str, object], rows: list[dict[str, object]] | None = None):
    def fake(url: str, params: dict[str, object] | None = None, **_kwargs: object) -> object:
        if "push2" in url:
            return _blocked()
        seen.update({"url": url, **(params or {})})
        return {"success": True, "result": {"count": 5015, "data": rows if rows is not None else [DATACENTER_ROW]}}

    return fake


def test_stock_scope_ranking_falls_back_to_datacenter(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(funds, "em_get", _fund_flow_em_get(seen))
    monkeypatch.setattr(funds, "request", lambda *_a, **_k: pytest.fail("stock scopes must not hit Sina"))

    out = funds.fund_flow_ranking("all", page=2, page_size=50, _no_cache=True)

    assert out["meta"]["source"] == "eastmoney"
    assert out["meta"]["fallback_from"] == "push2"
    assert out["meta"]["degraded"] == "eod_datacenter"
    assert out["meta"]["data_as_of"] == "2026-10-23"
    assert "中单" in out["meta"]["note"]
    assert seen["reportName"] == "RPT_DMSK_TS_STOCKNEW"
    assert seen["pageNumber"] == 2 and seen["pageSize"] == 50
    assert seen["sortColumns"] == "PRIME_INFLOW" and seen["sortTypes"] == "-1"
    assert seen["filter"] == (
        '(TRADE_MARKET_CODE in ("069001001001","069001001006","069001002001","069001002002"))'
    )
    assert out["data"]["total"] == 5015
    assert out["data"]["page"] == 2 and out["data"]["page_size"] == 50

    item = out["data"]["items"][0]
    assert item["code"] == "000001"
    assert item["name"] == "平安银行"
    assert item["price"] == 11.2
    assert item["change_pct"] == -0.885
    assert item["main_net"] == -167603481.0
    assert item["super_large_net"] == -130025385.0
    assert item["large_net"] == -37578096.0
    assert item["medium_net"] is None and item["small_net"] is None
    turnover = 202485751 / 0.1244
    assert item["main_net_pct"] == pytest.approx(-167603481 / turnover * 100, abs=1e-4)
    assert item["super_large_pct"] == pytest.approx(-130025385 / turnover * 100, abs=1e-4)
    assert item["large_pct"] == pytest.approx(-37578096 / turnover * 100, abs=1e-4)


@pytest.mark.parametrize(
    ("scope", "markets", "absent"),
    [
        ("sh", ("069001001001", "069001001006"), ("069001002001", "069001002002")),
        ("sz", ("069001002001", "069001002002"), ("069001001001", "069001001006")),
    ],
)
def test_stock_scope_ranking_filters_the_requested_market(
    monkeypatch: pytest.MonkeyPatch, scope: str, markets: tuple[str, ...], absent: tuple[str, ...]
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(funds, "em_get", _fund_flow_em_get(seen))
    monkeypatch.setattr(funds, "request", lambda *_a, **_k: pytest.fail("stock scopes must not hit Sina"))

    out = funds.fund_flow_ranking(scope, _no_cache=True)

    flt = str(seen["filter"])
    for market in markets:
        assert market in flt
    for market in absent:
        assert market not in flt
    assert out["data"]["scope"] == scope


def test_stock_scope_ranking_reports_an_empty_substitute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(funds, "em_get", _fund_flow_em_get({}, rows=[]))
    monkeypatch.setattr(funds, "request", lambda *_a, **_k: pytest.fail("stock scopes must not hit Sina"))

    out = funds.fund_flow_ranking("all", _no_cache=True)

    assert out["ok"] is True
    assert out["data"] == []
    assert out["meta"]["degraded"] == "eod_datacenter"
    assert "无数据" in out["meta"]["note"]


def test_stock_scope_ranking_raises_the_push2_error_when_both_sources_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(funds, "em_get", _blocked)
    monkeypatch.setattr(funds, "request", lambda *_a, **_k: pytest.fail("stock scopes must not hit Sina"))

    with pytest.raises(TickHubError) as exc:
        funds.fund_flow_ranking("all", _no_cache=True)

    assert exc.value.code == "QFSD-7001"


# --------------------------------------------------------------------------- #
# Board roster
# --------------------------------------------------------------------------- #


def test_industry_boards_fall_back_to_sina_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_request(_source: str, _method: str, url: str, **kwargs: object) -> object:
        seen.update({"url": url, **(kwargs.get("params") or {})})
        return SINA_ROSTER

    monkeypatch.setattr(market, "_clist", _blocked)
    monkeypatch.setattr(market, "request", fake_request)

    out = market.industry_boards(page=1, page_size=1)

    assert seen["url"] == market._SINA_INDUSTRY
    assert out["meta"]["source"] == "sina"
    assert out["meta"]["fallback_from"] == "eastmoney"
    assert out["data"]["total"] == 2
    assert len(out["data"]["items"]) == 1
    row = out["data"]["items"][0]
    assert row["board_code"] == "new_jrhy"
    assert row["board_name"] == "金融服务"
    assert row["change_pct"] == 1.54
    assert row["member_count"] == 50
    assert row["leader_code"] == "sh600000"
    assert row["main_net_inflow"] is None


def test_concept_boards_fall_back_with_class_param(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_request(_source: str, _method: str, url: str, **kwargs: object) -> object:
        seen.update({"url": url, **(kwargs.get("params") or {})})
        return SINA_ROSTER

    monkeypatch.setattr(market, "_clist", _blocked)
    monkeypatch.setattr(market, "request", fake_request)

    out = market.concept_boards(page=2, page_size=1)

    assert seen["url"] == market._SINA_CONCEPT
    assert seen["param"] == "class"
    assert out["data"]["page"] == 2
    assert out["data"]["items"][0]["board_code"] == "new_dzxx"


def test_sina_roster_pagination_out_of_range_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market, "_clist", _blocked)
    monkeypatch.setattr(market, "request", lambda *_a, **_k: SINA_ROSTER)

    out = market.industry_boards(page=9, page_size=100)

    assert out["ok"] is True
    assert out["data"] == []
    assert out["meta"]["note"] == "新浪板块列表不可用"


# --------------------------------------------------------------------------- #
# Board members
# --------------------------------------------------------------------------- #

BOARD_CONSTITUENT_ROW = {
    "BOARD_CODE": "475",
    "BOARD_CODE_BK": "BK0475",
    "BOARD_NAME": "银行Ⅱ",
    "SECURITY_CODE": "000001",
    "SECUCODE": "000001.SZ",
    "NEWEST_DATE": "2026-10-23 00:00:00",
}

TENCENT_QUOTE_ROW = {
    "code": "000001",
    "name": "平安银行",
    "price": 11.2,
    "change": -0.1,
    "change_pct": -0.89,
    "volume": 123456,
    "amount": 98765.4,
    "turnover_rate": 1.2,
    "pe": 5.1,
    "pb": 0.6,
    "float_market_cap": 200000.0,
    "total_market_cap": 210000.0,
}


def _board_datacenter(seen: dict[str, object], rows: list[dict[str, object]] | None = None):
    def fake(url: str, params: dict[str, object] | None = None, **_kwargs: object) -> object:
        seen.update({"url": url, **(params or {})})
        return {
            "success": True,
            "result": {"count": 42, "data": rows if rows is not None else [BOARD_CONSTITUENT_ROW]},
        }

    return fake


def test_board_members_fall_back_to_datacenter_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(market, "_clist", _blocked)
    monkeypatch.setattr(market, "em_get", _board_datacenter(seen))
    monkeypatch.setattr(quotes, "tencent_quote", lambda *_a, **_k: {"data": [TENCENT_QUOTE_ROW]})

    out = market.board_members("BK0475", page=1, page_size=50, _no_cache=True)

    assert seen["reportName"] == "RPT_BOARD_CONSTITUENT"
    # The report keys boards by the numeric suffix; "BK0475" filters to nothing.
    assert seen["filter"] == '(BOARD_CODE="475")'
    assert seen["pageNumber"] == 1 and seen["pageSize"] == 50
    assert out["meta"]["fallback_from"] == "push2"
    assert out["meta"]["degraded"] == "eod_constituent"
    assert out["meta"]["data_as_of"] == "2026-10-23"
    assert out["meta"]["amount_unit"] == "wan_yuan"
    assert out["data"]["total"] == 42
    item = out["data"]["items"][0]
    assert item["code"] == "000001"
    assert item["name"] == "平安银行"
    assert item["price"] == 11.2
    assert item["change_pct"] == -0.89
    assert item["main_net_inflow"] is None and item["main_net_ratio"] is None


def test_board_members_reject_a_substitute_board(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(market, "_clist", _blocked)
    monkeypatch.setattr(
        market,
        "em_get",
        _board_datacenter(seen, rows=[{**BOARD_CONSTITUENT_ROW, "BOARD_CODE_BK": "BK0476"}]),
    )
    monkeypatch.setattr(quotes, "tencent_quote", lambda *_a, **_k: {"data": []})

    with pytest.raises(TickHubError) as exc:
        market.board_members("BK0475", _no_cache=True)

    assert exc.value.code == "QFSD-7001"


def test_board_members_substitute_empty_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market, "_clist", _blocked)
    monkeypatch.setattr(
        market,
        "em_get",
        lambda *_a, **_k: {"success": False, "message": "返回数据为空"},
    )
    monkeypatch.setattr(quotes, "tencent_quote", lambda *_a, **_k: pytest.fail("no members, no quotes"))

    out = market.board_members("BK9999", _no_cache=True)

    assert out["ok"] is True
    assert out["data"] == []
    assert out["meta"]["degraded"] == "eod_constituent"


# --------------------------------------------------------------------------- #
# Index roster
# --------------------------------------------------------------------------- #


def test_index_list_falls_back_to_tencent_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: dict[str, object] = {}

    def fake_quote(codes: str) -> dict[str, object]:
        asked["codes"] = codes
        return {
            "data": [
                {"code": "000001", "name": "上证指数", "price": 3730.92, "change_pct": 0.63,
                 "change": 23.4, "volume": 417791248, "amount": 5000000.0},
                {"code": "000300", "name": "沪深300", "price": 4500.0, "change_pct": 0.5,
                 "change": 22.0, "volume": 100000, "amount": 900000.0},
            ]
        }

    monkeypatch.setattr(indices, "em_get", _blocked)
    monkeypatch.setattr(quotes, "tencent_quote", fake_quote)

    out = indices.index_list("sh", _no_cache=True)

    assert "sh000001" in str(asked["codes"])
    assert "sz399001" not in str(asked["codes"])
    assert out["meta"]["source"] == "tencent"
    assert out["meta"]["fallback_from"] == "eastmoney"
    assert out["meta"]["degraded"] == "headline_roster"
    assert "非全量列表" in out["meta"]["note"]
    assert out["data"]["scope"] == "sh"
    assert out["data"]["indices"][0]["code"] == "000001"


def test_index_list_all_scope_spans_both_markets(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: dict[str, object] = {}

    def fake_quote(codes: str) -> dict[str, object]:
        asked["codes"] = codes
        return {"data": [{"code": "000001", "name": "上证指数", "price": 1.0}]}

    monkeypatch.setattr(indices, "em_get", _blocked)
    monkeypatch.setattr(quotes, "tencent_quote", fake_quote)

    indices.index_list("all", _no_cache=True)

    assert "sh000001" in str(asked["codes"]) and "sz399001" in str(asked["codes"])


def test_index_list_reports_an_empty_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indices, "em_get", _blocked)
    monkeypatch.setattr(quotes, "tencent_quote", lambda *_a, **_k: {"data": []})

    out = indices.index_list("sz", _no_cache=True)

    assert out["ok"] is True
    assert out["data"] == []
    assert out["meta"]["degraded"] == "headline_roster"


# --------------------------------------------------------------------------- #
# Series with no zero-auth substitute
# --------------------------------------------------------------------------- #


def test_board_history_preserves_the_push2_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market, "em_get", _blocked)

    with pytest.raises(TickHubError) as exc:
        market.board_history("BK0475", _no_cache=True)

    assert exc.value.code == "QFSD-7001"


def test_sw_industry_index_preserves_the_push2_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indices, "em_get", _blocked)

    with pytest.raises(TickHubError) as exc:
        indices.sw_industry_index("801010", _no_cache=True)

    assert exc.value.code == "QFSD-7001"
