"""push2 quote paths fall back to non-push2 sources instead of failing.

The upstream blocks a source IP for minutes once it bursts one quote path, so
capital-flow and board tools must keep answering from Sina. Each case pins the
substitute payload shape that was verified live.
"""

from __future__ import annotations

import pytest

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import funds, market


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


def test_stock_scope_has_no_sina_substitute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(funds, "em_get", _blocked)
    monkeypatch.setattr(funds, "request", lambda *_a, **_k: pytest.fail("stock scopes must not hit Sina"))

    with pytest.raises(TickHubError) as exc:
        funds.fund_flow_ranking("all")

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
