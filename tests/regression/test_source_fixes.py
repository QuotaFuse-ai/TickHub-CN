"""Regressions for upstream endpoint changes found by the live sweep.

Each case pins the payload shape that a fixed source actually returns, so a
future endpoint drift fails here instead of surfacing as an empty result.
"""

from __future__ import annotations

import pytest

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import announcements, emdc, financials, funds
from tickhub_cn.risk.events import classify_announcement, from_announcements


# --------------------------------------------------------------------------- #
# Eastmoney news feeds
# --------------------------------------------------------------------------- #


def test_stock_news_reads_the_list_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_em_get(url: str, **kwargs: object) -> dict[str, object]:
        calls.append({"url": url, **kwargs})
        return {
            "data": {
                "list": [
                    {
                        "Art_ShowTime": "2026-10-23 18:00:00",
                        "Art_Title": "<em>浦发银行</em>三季度业绩说明会",
                        "Art_Url": "https://finance.eastmoney.com/a/1.html",
                        "Art_OriginUrl": "https://finance.eastmoney.com/a/1.html",
                    }
                ]
            }
        }

    monkeypatch.setattr(announcements, "em_get", fake_em_get)

    out = announcements.eastmoney_stock_news("600000", limit=5)

    rows = out["data"]["news"]
    assert rows[0]["title"] == "浦发银行三季度业绩说明会"
    assert rows[0]["media"] == "finance.eastmoney.com"
    assert calls[0]["params"]["mTypeAndCode"] == "1.600000"  # type: ignore[index]


def test_global_news_sends_a_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_em_get(_url: str, **kwargs: object) -> dict[str, object]:
        seen.update(kwargs.get("params") or {})
        return {"data": {"fastNewsList": [{"code": "1", "title": "t", "summary": "s", "showTime": "now"}]}}

    monkeypatch.setattr(announcements, "em_get", fake_em_get)

    out = announcements.eastmoney_global_news(limit=1)

    assert out["meta"]["count"] == 1
    # The feed rejects a request with no req_trace.
    assert str(seen["req_trace"]).isdigit()


# --------------------------------------------------------------------------- #
# CCTV Xinwen Lianbo markup
# --------------------------------------------------------------------------- #


def test_cctv_news_reads_title_attributes_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    html = (
        '<li><a href="https://tv.cctv.com/2026/10/23/A.shtml" title="习近平会见外宾"><img src="x"></a></li>'
        '<li><a href="https://tv.cctv.com/2026/10/23/A.shtml" title="习近平会见外宾">习近平会见外宾</a></li>'
        '<li><a href="https://tv.cctv.com/2026/10/23/B.shtml" title="国内联播快讯">国内联播快讯</a></li>'
        '<li><a href="/plain">no title attribute</a></li>'
    )
    monkeypatch.setattr(announcements, "request", lambda *a, **k: html)

    out = announcements.cctv_news("2026-10-23")

    items = out["data"]["items"]
    assert [item["title"] for item in items] == ["习近平会见外宾", "国内联播快讯"]
    assert out["data"]["date"] == "2026-10-23"
    assert out["meta"]["count"] == 2


# --------------------------------------------------------------------------- #
# Datacenter "no rows" is an empty answer, not a broken interface
# --------------------------------------------------------------------------- #


def test_datacenter_empty_result_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        emdc,
        "em_get",
        lambda *a, **k: {"success": False, "message": "返回数据为空", "code": 9201},
    )

    assert emdc.fetch_pages("RPT_X", "(A=1)", "A", "-1", 10) == []


def test_datacenter_real_failure_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emdc, "em_get", lambda *a, **k: {"success": False, "message": "服务异常"})

    with pytest.raises(TickHubError) as excinfo:
        emdc.fetch_pages("RPT_X", "(A=1)", "A", "-1", 10)
    assert excinfo.value.code == "QFSD-9003"


# --------------------------------------------------------------------------- #
# CNINFO risk events
# --------------------------------------------------------------------------- #


def test_cninfo_queries_with_a_blank_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank ``column`` lets CNINFO apply each board's own filing rules.

    Pinning ``szse`` dropped Shenzhen 投资者关系管理信息 records; the query is
    filtered by ``stock``, so ordering is unaffected either way.
    """
    posted: list[dict[str, object]] = []

    def fake_request(_source: str, _method: str, _url: str, **kwargs: object) -> object:
        payload = kwargs.get("data")
        if isinstance(payload, dict) and "keyWord" in payload:
            return [{"code": "600000", "orgId": "gssh0600000"}]
        posted.append(dict(payload or {}))
        return {"announcements": [], "totalAnnouncement": 0}

    monkeypatch.setattr(announcements, "request", fake_request)

    announcements.cninfo_announcements("600000", start="2026-07-01", end="2026-10-25")

    query = posted[-1]
    assert query["column"] == ""
    assert query["stock"] == "600000,gssh0600000"
    assert query["plate"] == ""


def test_risk_events_take_the_cninfo_publish_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        announcements,
        "_cninfo_rows",
        lambda *a, **k: (
            [
                {"title": "关于收到《行政处罚决定书》的公告", "publish_time": "2026-10-15 00:00:00"},
                {"title": "2026年半年度报告", "publish_time": "2026-08-28 00:00:00"},
            ],
            2,
        ),
    )

    out = announcements.cninfo_announcement_risk("600010")

    events = out["data"]["events"]
    assert out["data"]["announcements"] == 2
    assert len(events) == 1
    assert events[0]["type"] == "regulatory_penalty"
    assert events[0]["date"] == "2026-10-15 00:00:00"


def test_risk_classifier_ignores_routine_notices() -> None:
    assert classify_announcement("关于召开2026年半年度业绩说明会的公告") is None
    assert classify_announcement("关于股东减持股份的公告")[0] == "share_reduction"


def test_from_announcements_falls_back_to_publish_time() -> None:
    events = from_announcements("600010", [{"title": "股份解除质押公告", "publish_time": "2026-10-21"}])

    assert events[0]["type"] == "equity_pledge"
    assert events[0]["date"] == "2026-10-21"


# --------------------------------------------------------------------------- #
# ETF share history requires the fund-site Referer
# --------------------------------------------------------------------------- #


def test_etf_share_history_sends_referer(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_request(_source: str, _method: str, _url: str, **kwargs: object) -> str:
        seen.update(kwargs)
        return "<tr><td>2026-09-30</td><td>12.34</td></tr>"

    monkeypatch.setattr(funds, "request", fake_request)

    out = funds.etf_share_history("510300", page_size=1)

    assert out["data"]["records"][0]["shares"] == 12.34
    assert seen["headers"]["Referer"] == "https://fundf10.eastmoney.com/"  # type: ignore[index]


# --------------------------------------------------------------------------- #
# Sina financial statements (JSON open API)
# --------------------------------------------------------------------------- #


def _sina_payload() -> dict[str, object]:
    return {
        "result": {
            "status": {"code": 0},
            "data": {
                "report_date": [{"date_value": "20260630", "date_description": "2026半年报"}],
                "report_list": {
                    "20260630": {
                        "data": [
                            {"item_title": "常用指标", "item_value": ""},
                            {"item_title": "营业总收入", "item_value": "92278072083.21", "item_tongbi": 0.013},
                            {"item_title": "净利润", "item_value": "46033330566.78"},
                            {"item_title": "商誉", "item_value": None},
                        ]
                    }
                },
            },
        }
    }


def test_sina_financial_report_parses_report_list(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_request(_source: str, _method: str, _url: str, **kwargs: object) -> dict[str, object]:
        seen.update(kwargs.get("params") or {})
        return _sina_payload()

    monkeypatch.setattr(financials, "request", fake_request)

    out = financials.sina_financial_report("600519", statement="income", periods=4, _no_cache=True)

    reports = out["data"]["reports"]
    assert reports[0]["report_date"] == "2026-06-30"
    assert reports[0]["report_name"] == "2026半年报"
    assert reports[0]["values"] == {"营业总收入": 92278072083.21, "净利润": 46033330566.78}
    assert reports[0]["yoy"] == {"营业总收入": 0.013}
    assert seen["paperCode"] == "sh600519"
    assert seen["source"] == "lrb"


def test_sina_financial_report_rejects_unknown_statement() -> None:
    with pytest.raises(TickHubError) as excinfo:
        financials.sina_financial_report("600519", statement="ebitda", _no_cache=True)
    assert excinfo.value.code == "QFSD-9001"


def test_sina_financial_report_skips_bse() -> None:
    out = financials.sina_financial_report("430047", _no_cache=True)
    assert out["ok"] is True
    assert out["data"] == []
    assert "BSE" in out["meta"]["note"]


# --------------------------------------------------------------------------- #
# Limit pools: per-kind sort field and trading-day default
# --------------------------------------------------------------------------- #

_POOL_ROW = {
    "c": "600000",
    "n": "浦发银行",
    "m": 1,
    "p": 10000,
    "zdp": 10.0,
    "lbc": 2,
    "fbt": "093000",
    "lbt": "093000",
    "fund": 1.5e8,
    "ltsz": 2.0e11,
    "tshare": 2.1e11,
    "hs": 1.2,
    "hybk": "银行",
    "zttj": {"days": 2, "ct": 1},
}


def _pool_em_get(seen: list[dict[str, object]]):
    def fake(url: str, **kwargs: object) -> dict[str, object]:
        seen.append({"url": url, **kwargs})
        return {"data": {"pool": [_POOL_ROW]}}

    return fake


def test_each_topic_pool_sends_its_own_sort_field(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import market

    seen: list[dict[str, object]] = []
    monkeypatch.setattr(market, "em_get", _pool_em_get(seen))

    for kind, endpoint, sort in (
        ("limit_up", "getTopicZTPool", "fbt:asc"),
        ("broken", "getTopicZBPool", "fbt:asc"),
        ("limit_down", "getTopicDTPool", "zdp:asc"),
        ("strong", "getTopicQSPool", "zdp:desc"),
        ("sub_new", "getTopicCXPool", "zdp:desc"),
    ):
        market._topic_pool(kind, "2026-10-22")
        assert seen[-1]["url"].endswith(endpoint)  # type: ignore[union-attr]
        assert seen[-1]["params"]["sort"] == sort  # type: ignore[index]
        assert seen[-1]["params"]["date"] == "20261022"  # type: ignore[index]


def test_topic_pool_defaults_to_the_last_trading_day(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import market

    seen: list[dict[str, object]] = []
    monkeypatch.setattr(market, "em_get", _pool_em_get(seen))
    monkeypatch.setattr(market, "last_trading_day", lambda: "2026-10-23")

    out = market.limit_down_pool()

    assert seen[0]["params"]["date"] == "20261023"  # type: ignore[index]
    assert out["data"]["date"] == "20261023"


def test_market_sentiment_reports_the_trading_day_it_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import market

    monkeypatch.setattr(market, "em_get", _pool_em_get([]))
    monkeypatch.setattr(market, "last_trading_day", lambda: "2026-10-23")

    out = market.market_sentiment(_no_cache=True)

    assert out["data"]["date"] == "20261023"
    assert out["data"]["limit_up"] == 1


# --------------------------------------------------------------------------- #
# Baidu MA fallback when the primary host is blocked
# --------------------------------------------------------------------------- #


def test_baidu_kline_falls_back_to_tencent_and_derives_mas(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import quotes

    closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    rows = [{"date": f"2026-10-{i + 1:02d}", "open": c, "close": c, "high": c, "low": c, "volume": 1} for i, c in enumerate(closes)]
    monkeypatch.setattr(quotes, "request", lambda *a, **k: (_ for _ in ()).throw(TickHubError("QFSD-9003", "blocked")))
    monkeypatch.setattr(quotes, "tencent_kline", lambda *a, **k: {"data": {"klines": [dict(r) for r in rows]}})

    out = quotes.baidu_kline_with_ma("600000", limit=3, _no_cache=True)

    klines = out["data"]["klines"]
    assert out["meta"]["source"] == "tencent"
    assert out["meta"]["fallback_from"] == "baidu"
    assert len(klines) == 3
    assert klines[-1]["ma5"] == 13.0  # (11+12+13+14+15)/5
    assert klines[-1]["ma10"] is None


# --------------------------------------------------------------------------- #
# Index constituents: datacenter report mapping
# --------------------------------------------------------------------------- #

_COMPONENT_ROW = {
    "SECUCODE": "000001.SZ",
    "TYPE": "1",
    "SECURITY_CODE": "000001",
    "SECURITY_NAME_ABBR": "平安银行",
    "CLOSE_PRICE": 11.2,
    "CHANGE_RATE": -0.8849,
    "MAXTRADEDATE": "2026-10-23 00:00:00",
    "INDUSTRY": "银行Ⅱ",
    "WEIGHT": 0.48,
    "EPS": 1.94,
    "BPS": 24.56,
    "TOTAL_SHARES": 194.06,
    "FREE_SHARES": 194.06,
    "FREE_CAP": 2173.44,
    "PE": 4.16,
}


def test_index_constituents_reads_the_datacenter_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import indices

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_strict(*args: object, **kwargs: object) -> list[dict[str, object]]:
        calls.append((args, kwargs))
        return [dict(_COMPONENT_ROW)]

    monkeypatch.setattr(indices, "strict_rows", fake_strict)

    out = indices.index_constituents("000300", _no_cache=True)

    args, kwargs = calls[0]
    assert args[0] == "RPT_INDEX_TS_COMPONENT"
    assert args[1] == '(TYPE="1")'
    assert kwargs["equal"] == {"TYPE": "1"}
    row = out["data"]["constituents"][0]
    assert row["code"] == "000001"
    assert row["exchange"] == "SZ"
    assert row["weight"] == 0.48
    assert row["as_of"] == "2026-10-23"
    assert out["meta"]["data_as_of"] == "2026-10-23"


def test_index_constituents_answers_unmapped_indices_with_a_note(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import indices

    monkeypatch.setattr(indices, "strict_rows", lambda *a, **k: pytest.fail("should not fetch"))

    out = indices.index_constituents("399001", _no_cache=True)

    assert out["data"] == []
    assert "尚未纳入成分表" in out["meta"]["note"]
    assert "000300" in out["meta"]["note"]


def test_index_constituents_rejects_stock_codes() -> None:
    from tickhub_cn.datasources import indices

    with pytest.raises(TickHubError) as excinfo:
        indices.index_constituents("600000", _no_cache=True)
    assert excinfo.value.code == "QFSD-9001"


def test_index_weights_notes_missing_weight_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import indices

    monkeypatch.setattr(
        indices,
        "index_constituents",
        lambda *a, **k: {"ok": True, "data": {"index_code": "000510", "constituents": [{"code": "000001", "name": "平安银行", "weight": None}]}, "meta": {}},
    )

    out = indices.index_weights("000510", _no_cache=True)

    assert out["data"] == []
    assert "未发布该指数权重" in out["meta"]["note"]


def test_index_weights_sums_published_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import indices

    monkeypatch.setattr(
        indices,
        "index_constituents",
        lambda *a, **k: {
            "ok": True,
            "data": {
                "index_code": "000300",
                "constituents": [
                    {"code": "600519", "name": "贵州茅台", "weight": 3.5, "as_of": "2026-10-23"},
                    {"code": "000001", "name": "平安银行", "weight": 0.48, "as_of": "2026-10-23"},
                ],
            },
            "meta": {},
        },
    )

    out = indices.index_weights("000300", _no_cache=True)

    assert [w["code"] for w in out["data"]["weights"]] == ["600519", "000001"]
    assert out["meta"]["covered"] == 2
    assert out["meta"]["total_weight"] == 3.98


# --------------------------------------------------------------------------- #
# THS hot list only answers a query string
# --------------------------------------------------------------------------- #


def test_ths_hot_rank_sends_a_get_with_query_params(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import market

    seen: dict[str, object] = {}

    def fake_request(source: str, method: str, url: str, **kwargs: object) -> dict[str, object]:
        seen.update({"source": source, "method": method, "url": url, **kwargs})
        return {
            "status_code": 0,
            "data": {
                "stock_list": [
                    {"market": 17, "code": "688836", "name": "宇树科技", "rate": "153143.0", "rise_and_fall": 20.0, "analyse": "机器人"}
                ]
            },
        }

    monkeypatch.setattr(market, "request", fake_request)

    out = market.ths_hot_rank(_no_cache=True)

    assert seen["method"] == "GET"
    assert seen["params"] == {"stock_type": "a", "type": "hour", "list_type": "normal"}  # type: ignore[index]
    row = out["data"][0]
    assert row["rank"] == 1 and row["code"] == "688836"
    assert row["heat"] == 153143.0
    assert out["meta"]["list_type"] == "hour"


def test_ths_hot_rank_notes_a_rejected_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import market

    monkeypatch.setattr(
        market,
        "request",
        lambda *a, **k: {"status_code": -1, "status_msg": "Param Error.", "data": {}},
    )

    out = market.ths_hot_rank(_no_cache=True)

    assert out["data"] == []
    assert "Param Error." in out["meta"]["note"]
    assert out["meta"]["status_code"] == -1


# --------------------------------------------------------------------------- #
# Eastmoney report API keys industries by numeric id
# --------------------------------------------------------------------------- #


_REPORT_ROW = {
    "title": "半导体行业跟踪",
    "stockCode": "000938",
    "stockName": "紫光股份",
    "orgSName": "某证券",
    "researcher": "张三",
    "emRatingName": "买入",
    "publishDate": "2026-10-24 00:00:00.000",
    "infoCode": "AP202610241830875755",
}


def test_industry_reports_filter_by_numeric_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import research

    seen: dict[str, object] = {}

    def fake_request(_source: str, _method: str, _url: str, **kwargs: object) -> dict[str, object]:
        seen.update(kwargs.get("params") or {})
        return {"hits": 61, "data": [_REPORT_ROW]}

    monkeypatch.setattr(research, "request", fake_request)

    out = research.eastmoney_industry_reports("447", page_size=1, _no_cache=True)

    assert seen["industryCode"] == "447"
    assert out["meta"]["count"] == 1
    assert out["data"]["reports"][0]["code"] == "000938"


def test_industry_reports_note_board_codes_instead_of_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import research

    monkeypatch.setattr(research, "request", lambda *a, **k: pytest.fail("board codes hit 0 rows upstream"))

    out = research.eastmoney_industry_reports("BK0475", _no_cache=True)

    assert out["data"] == []
    assert "447" in out["meta"]["note"] and "BK0475" in out["meta"]["note"]


def test_industry_reports_require_an_industry() -> None:
    from tickhub_cn.datasources import research

    with pytest.raises(TickHubError) as excinfo:
        research.eastmoney_industry_reports("", _no_cache=True)
    assert excinfo.value.code == "QFSD-9001"


def test_stock_reports_keep_rows_when_the_industry_filter_is_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import research

    seen: dict[str, object] = {}

    def fake_request(_source: str, _method: str, _url: str, **kwargs: object) -> dict[str, object]:
        seen.update(kwargs.get("params") or {})
        return {"hits": 1, "data": [_REPORT_ROW]}

    monkeypatch.setattr(research, "request", fake_request)

    out = research.eastmoney_reports("000938", industry_code="BK0475", _no_cache=True)

    assert seen["industryCode"] == "*"
    assert len(out["data"]["reports"]) == 1
    assert "BK0475" in out["meta"]["note"]


# --------------------------------------------------------------------------- #
# Exchange publication timing / explicit empty notes
# --------------------------------------------------------------------------- #


def test_szse_margin_empty_result_names_the_next_day_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tickhub_cn.datasources import backups

    monkeypatch.setattr(backups, "_szse_margin_rows", lambda *_a, **_k: [])

    out = backups.margin_trading_backup(trade_date="2026-10-23", exchange="SZSE", _no_cache=True)

    assert out["data"] == []
    assert "T+1" in out["meta"]["note"]
    assert "2026-10-22" in out["meta"]["note"]


def test_szse_margin_defaults_a_day_earlier_but_keeps_explicit_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tickhub_cn.datasources import backups

    monkeypatch.setattr(
        backups, "last_trading_day", lambda anchor=None: "2026-10-23" if anchor is None else "2026-10-22"
    )
    monkeypatch.setattr(backups, "_szse_margin_summary", lambda day: None)
    seen: list[str] = []

    def szse_rows(day, *_a, **_k):
        seen.append(day)
        return [{"zqdm": "000001", "zqjc": "平安银行"}]

    monkeypatch.setattr(backups, "_szse_margin_rows", szse_rows)

    default_out = backups.margin_trading_backup(_no_cache=True)
    assert seen == ["2026-10-22"], "default SZSE call must start one trading day earlier"
    assert default_out["data"]["date"] == "2026-10-22"
    assert "回退" in default_out["meta"]["note"]

    explicit = backups.margin_trading_backup(trade_date="2026-10-21", _no_cache=True)
    assert seen[-1] == "2026-10-21"
    assert "note" not in explicit["meta"], "an explicit date is never shifted"

    monkeypatch.setattr(
        backups,
        "_sse_margin_rows",
        lambda day: seen.append(day) or [{"stockCode": "600000", "securityAbbr": "浦发银行"}],
    )
    backups.margin_trading_backup(exchange="SSE", _no_cache=True)
    assert seen[-1] == "2026-10-23", "SSE publishes same-day, so its default stays put"


def test_sse_interaction_keeps_the_sites_no_answer_note(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import irm

    page = (
        '<div class="center"><a href="javascript:;" class="m_feed_note">近1个月暂无回复</a></div>'
    )
    monkeypatch.setattr(irm, "_sse_company_uid", lambda _code: "65")
    monkeypatch.setattr(irm, "request", lambda *_a, **_k: page)

    out = irm.sse_e_interaction("600000", _no_cache=True)

    assert out["data"] == []
    assert out["meta"]["note"] == "近1个月暂无回复"
    assert out["meta"]["code"] == "600000"


def test_sse_interaction_rejects_a_silent_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import irm

    monkeypatch.setattr(irm, "_sse_company_uid", lambda _code: "65")
    monkeypatch.setattr(irm, "request", lambda *_a, **_k: "<div>nothing here</div>")

    with pytest.raises(TickHubError) as excinfo:
        irm.sse_e_interaction("600000", _no_cache=True)
    assert excinfo.value.code == "QFSD-9003"


# --------------------------------------------------------------------------- #
# SSE e-Interaction relative timestamps (V3.10.3)
# --------------------------------------------------------------------------- #

def _fixed_shanghai():
    import datetime as dt

    return dt.datetime(2026, 10, 25, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def test_sse_time_normalises_relative_and_absolute_stamps() -> None:
    import datetime as dt

    from tickhub_cn.datasources import irm

    now = dt.datetime(2026, 10, 25, 10, 0)
    cases = {
        "昨天 18:22": "2026-10-24 18:22",
        "今天 09:05": "2026-10-25 09:05",
        "前天 23:59": "2026-10-23 23:59",
        "2026-10-23 18:22": "2026-10-23 18:22",
        "2026年10月23日 18:22": "2026-10-23 18:22",
        "10-22 18:22": "2026-10-22 18:22",
        "3 分钟前": "2026-10-25 09:57",
        "2 小时前": "2026-10-25 08:00",
        "刚刚": "2026-10-25 10:00",
        "18:22": "2026-10-25 18:22",
    }
    for text, expected in cases.items():
        assert irm._sse_time(text, now=now) == expected, text
    assert irm._sse_time("昨天 25:00", now=now) is None
    assert irm._sse_time("", now=now) is None
    assert irm._sse_time("不是时间", now=now) is None


def test_sse_time_rolls_a_future_month_day_back_a_year() -> None:
    import datetime as dt

    from tickhub_cn.datasources import irm

    now = dt.datetime(2026, 1, 3, 10, 0)
    assert irm._sse_time("12-30 18:22", now=now) == "2025-12-30 18:22"


def test_sse_required_time_still_reports_unrecognised_text() -> None:
    from tickhub_cn.datasources import irm

    with pytest.raises(TickHubError) as excinfo:
        irm._sse_required_time("7", "某个新写法")
    assert excinfo.value.code == "QFSD-9003"
    assert "某个新写法" in str(excinfo.value)


def test_sse_feed_normalises_a_relative_answer_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tickhub_cn.datasources import irm

    monkeypatch.setattr(irm, "now_shanghai", _fixed_shanghai)
    page = (
        '<div class="m_feed_item" id="item-42">'
        '<div class="m_feed_txt"><a href="#">:浦发银行(600000)</a>请问三季度业绩如何？</div>'
        '<div class="m_feed_from"><span>2026-10-23 09:00</span></div>'
        '<div class="m_feed_detail m_qa">'
        '<div class="m_feed_txt">经营稳健，详见公告。</div>'
        '<div class="m_feed_from"><span>昨天 18:22</span></div>'
        "</div></div>"
    )

    rows = irm._sse_parse_feed(page)

    assert rows[0]["question_time"] == "2026-10-23 09:00"
    assert rows[0]["answer_time"] == "2026-10-24 18:22"


def test_sse_feed_refuses_an_unparseable_reply_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from tickhub_cn.datasources import irm

    monkeypatch.setattr(irm, "now_shanghai", _fixed_shanghai)
    page = (
        '<div class="m_feed_item" id="item-7">'
        '<div class="m_feed_txt"><a href="#">:浦发银行(600000)</a>问题</div>'
        '<div class="m_feed_from"><span>2026-10-23 09:00</span></div>'
        '<div class="m_feed_detail m_qa">'
        '<div class="m_feed_txt">回答</div>'
        '<div class="m_feed_from"><span>下次再说</span></div>'
        "</div></div>"
    )

    with pytest.raises(TickHubError) as excinfo:
        irm._sse_parse_feed(page)
    assert excinfo.value.code == "QFSD-9003"
