"""QUOT-368: ``realtime_fund_flow`` 的 eastmoney 通道必须与批量同端点、同量纲。

背景：该工具原来走 ``push2/api/qt/stock/get``，对 f62/f66/f72/f78/f84/f184 返回的是
另一套刻度。2026-11-04 12:26:19 实测 002696（百洋股份）**同一刻**：

    realtime_fund_flow  price=1112.0  main_net=3.0            main_net_pct=+11.01
    ulist（批量用的）    price=11.12   main_net=-171530812.0   main_net_pct=-30.63

不只是 ×100 的量纲问题，符号都相反，``f66``/``f72`` 直接缺。所以「补 fltt/invt」
只能修好 ``price``（1018 → 10.18），资金列仍然是错的——这条用例锁的是修完之后的行为：
eastmoney 与 sina 两条通道在同一只股票上的 ``main_net`` 必须**同量级**（比值接近 1，
不是 1e8），且分档列齐全。

夹具用的是上面那次实测的真实字段号与真实数值，不是编的。
"""

from __future__ import annotations

from typing import Any

import pytest

from tickhub_cn.core.errors import TickHubError
from tickhub_cn.datasources import funds

CODE = "002696"
# 2026-11-04 12:26:19 盘中 ulist 真实快照（百洋股份），字段号同线上请求
ULIST_ROW: dict[str, Any] = {
    "f12": CODE,
    "f14": "百洋股份",
    "f2": 11.12,
    "f3": -2.11,
    "f62": -171530812.0,
    "f66": -98000000.0,
    "f69": -17.50,
    "f72": -73530812.0,
    "f75": -13.13,
    "f78": 21000000.0,
    "f81": 3.75,
    "f84": 150530812.0,
    "f87": 26.88,
    "f184": -30.63,
}
# 同刻新浪实时资金流（口径接近，不逐分相等；netamount 与东财同量级同符号）
SINA_TEXT = (
    '[{"opendate":"2026-11-04","trade":"11.12","changeratio":"-0.0211",'
    '"netamount":"-171465000","r0_in":"12000000","r0_out":"110000000",'
    '"r1_in":"40000000","r1_out":"113530812",'
    '"r2_in":"121000000","r2_out":"100000000",'
    '"r3_in":"250530812","r3_out":"100000000","name":"百洋股份"}]'
)


def _ulist_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"rc": 0, "data": {"total": len(rows), "diff": rows}}


def _blocked(*_args: Any, **_kwargs: Any) -> Any:
    raise TickHubError("QFSD-7001", "endpoint breaker open")


def test_eastmoney_channel_asks_ulist_with_unit_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """端点仍是 stock/get 时这条必挂 —— 那正是 QUOT-368 的缺陷本身。"""
    seen: dict[str, Any] = {}

    def fake_em_get(url: str, params: dict[str, Any] | None = None, **_kw: Any) -> Any:
        seen["url"] = url
        seen["params"] = params or {}
        return _ulist_payload([ULIST_ROW])

    monkeypatch.setattr(funds, "em_get", fake_em_get)

    out = funds.realtime_fund_flow(CODE, _no_cache=True)

    assert seen["url"] == funds._ULIST
    assert seen["params"]["fltt"] == "2" and seen["params"]["invt"] == "2"
    requested = set(seen["params"]["fields"].split(","))
    # 主力 + 四档资金列都要在请求里，否则分档永远是 null
    for field in ("f62", "f184", "f66", "f69", "f72", "f75", "f78", "f81", "f84", "f87"):
        assert field in requested, field
    assert out["meta"]["source"] == "eastmoney"


def test_price_is_yuan_not_fen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(funds, "em_get", lambda url, params=None, **_k: _ulist_payload([ULIST_ROW]))

    data = funds.realtime_fund_flow(CODE, _no_cache=True)["data"]

    assert data["price"] == pytest.approx(11.12)  # 不是 1112.0


def test_main_net_and_pct_keep_the_upstream_sign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(funds, "em_get", lambda url, params=None, **_k: _ulist_payload([ULIST_ROW]))

    data = funds.realtime_fund_flow(CODE, _no_cache=True)["data"]

    assert data["main_net"] == pytest.approx(-171530812.0)
    assert data["main_net_pct"] == pytest.approx(-30.63)  # 负号必须保留
    assert data["main_net"] != 3.0  # 旧 stock/get 的退化值


def test_all_four_tiers_are_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(funds, "em_get", lambda url, params=None, **_k: _ulist_payload([ULIST_ROW]))

    data = funds.realtime_fund_flow(CODE, _no_cache=True)["data"]

    for key in ("super_large_net", "large_net", "medium_net", "small_net"):
        assert data[key] is not None, key
    for key in ("super_large_pct", "large_pct", "medium_pct", "small_pct"):
        assert data[key] is not None, key


def test_eastmoney_and_sina_main_net_are_same_magnitude(monkeypatch: pytest.MonkeyPatch) -> None:
    """本单验收第 2 条：两条通道同一只股票的 main_net 比值接近 1，不是 1e8。"""
    monkeypatch.setattr(funds, "em_get", lambda url, params=None, **_k: _ulist_payload([ULIST_ROW]))
    eastmoney = funds.realtime_fund_flow(CODE, _no_cache=True)["data"]

    monkeypatch.setattr(funds, "em_get", _blocked)
    monkeypatch.setattr(funds, "request", lambda *_a, **_k: SINA_TEXT)
    sina = funds.realtime_fund_flow(CODE, _no_cache=True)

    assert sina["meta"]["source"] == "sina"
    sina_data = sina["data"]

    assert eastmoney["main_net"] < 0 and sina_data["main_net"] < 0, "两条通道符号必须一致"
    ratio = abs(eastmoney["main_net"] / sina_data["main_net"])
    assert 0.9 <= ratio <= 1.1, f"main_net 不同量级：比值 {ratio:g}"
    # 元级金额落在亿级区间；回到 3.0 那类退化值会立刻挂在这条上
    assert 1e8 <= abs(eastmoney["main_net"]) < 1e10
