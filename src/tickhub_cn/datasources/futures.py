"""Futures and options: exchange daily files, position ranks, Sina quotes, SGE spot.

Only exchange-published files (SHFE / INE / CZCE / CFFEX / GFEX) are treated as
authoritative. Sina covers what the exchanges do not publish reliably, notably
DCE products and global / A50 contracts.

DCE is intentionally absent from the exchange layer: its website answers plain
HTTP clients with 412 (JavaScript anti-bot). Use ``futures_kline`` /
``futures_realtime`` for DCE products.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any
from xml.etree import ElementTree

from ..core.errors import TickHubError
from ..core.http import fetch, request
from ..core.ticker import parse_date
from .base import tool
from .util import frame, last_trading_day, to_float, to_int

EXCHANGES = ("SHFE", "INE", "CZCE", "CFFEX", "GFEX")

_SHFE_HOSTS = {"SHFE": "https://www.shfe.com.cn", "INE": "https://www.ine.cn"}
_CZCE_URL = "https://www.czce.com.cn/cn/DFSStaticFiles/{kind}/{year}/{ymd}/{name}.txt"
_CZCE_FIRST_DAY = "20150921"
_CFFEX_DAILY = "http://www.cffex.com.cn/fzjy/mrhq/{ym}/{dd}/{ymd}_1.csv"
_CFFEX_WITNESS = "http://www.cffex.com.cn/fzjy/mrhq/{ym}/{dd}/index.xml"
_CFFEX_RANK = "http://www.cffex.com.cn/sj/ccpm/{ym}/{dd}/{product}_1.csv"
_CFFEX_RANK_FIRST_DAY = {
    "IF": "20100416",
    "IH": "20150416",
    "IC": "20150416",
    "IM": "20220722",
    "TS": "20180817",
    "TF": "20130906",
    "T": "20150320",
    "TL": "20230421",
}
_GFEX_URL = "http://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList"
_GFEX_REFERER = "http://www.gfex.com.cn/gfex/rihq/hqsj_tjsj.shtml"
_SINA_HQ = "https://hq.sinajs.cn/list="
_SINA_KLINE = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{code}="
    "/InnerFuturesNewService.getDailyKLine"
)
_SGE_URL = "https://www.sge.com.cn/graph/Dailyhq"

# first day each INE file family exists (verified against the exchange archives)
_INE_FIRST_DAY = {
    "future/dailydata/kx": "20180326",
    "future/dailydata/pm": "20200703",
    "option/dailydata/kx": "20210621",
}

_CFFEX_SINA_PRODUCTS = ("IF", "IH", "IC", "IM", "TS", "TF", "T", "TL")
_DCE_HINT = (
    "大商所官网有 JS 反爬（纯 HTTP 返回 412），不提供官方日行情；"
    "大商所品种请用 futures_kline('M0') / futures_realtime('M0')（新浪）"
)

_FUT_COLUMNS = [
    "date", "exchange", "symbol", "product", "open", "high", "low", "close", "settle",
    "pre_settle", "volume", "open_interest", "oi_change", "turnover_10k",
]
_OPT_COLUMNS = [
    "date", "exchange", "symbol", "series", "option_type", "strike", "open", "high", "low",
    "close", "settle", "pre_settle", "volume", "open_interest", "oi_change", "turnover_10k",
    "delta", "iv_pct", "series_iv_pct",
]
_RANK_COLUMNS = [
    "date", "exchange", "level", "symbol", "rank", "volume_member", "volume", "volume_chg",
    "long_member", "long_oi", "long_chg", "short_member", "short_oi", "short_chg",
]
_KLINE_COLUMNS = ["date", "symbol", "open", "high", "low", "close", "settle", "volume", "open_interest"]

_OPTION_CODE = re.compile(r"^([A-Za-z]+\d{3,4}(?:[A-Z]{2})?)-?([CP])-?(\d+(?:\.\d+)?)$")
_FUT_SYMBOL = re.compile(r"^[A-Za-z]{1,2}\d{1,4}$")
_CZCE_HEADER_ALIAS = {"品种月份": "合约代码", "品种代码": "合约代码", "空盘量": "持仓量"}


def _fail(message: str, detail: Any = None) -> TickHubError:
    return TickHubError("QFSD-9003", message, detail=detail)


def _no_data(message: str, detail: Any = None) -> TickHubError:
    return TickHubError("QFSD-9005", message, detail=detail)


def _bad_arg(message: str) -> TickHubError:
    return TickHubError("QFSD-9001", message)


def _source(exchange: str) -> str:
    return exchange.lower()


def _price(value: Any) -> float | None:
    number = to_float(value)
    return None if number == 0 else number


def _need(row: dict[str, Any], key: str, context: str) -> Any:
    if key not in row or row[key] is None:
        raise _fail(f"{context} 缺少字段 {key}，接口格式可能已变")
    return row[key]


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _check_exchange(exchange: str) -> str:
    name = _text(exchange).upper()
    if name == "DCE":
        raise TickHubError("QFSD-9002", _DCE_HINT)
    if name not in EXCHANGES:
        raise _bad_arg(f"exchange 只能是 {' / '.join(EXCHANGES)} 或 ALL（大商所见 futures_kline / futures_realtime）")
    return name


def _day(date: str | None) -> tuple[str, str]:
    try:
        text = parse_date(date or last_trading_day(), last_trading_day())
    except ValueError as exc:
        raise _bad_arg(str(exc)) from exc
    return text, text.replace("-", "")


def _product_of(code: str) -> str:
    match = re.match(r"[A-Za-z]+", _text(code))
    if not match:
        raise _fail(f"合约代码 {code!r} 不是字母开头，格式可能已变")
    return match.group(0)


# --------------------------------------------------------------------------- #
# SHFE / INE (.dat files that are really JSON)
# --------------------------------------------------------------------------- #

def _shfe_payload(exchange: str, path: str, ymd: str, key: str) -> tuple[dict[str, Any] | None, str]:
    url = f"{_SHFE_HOSTS[exchange]}/data/tradedata/{path}{ymd}.dat"
    response = fetch(_source(exchange), "GET", url, allow_status=(404,), timeout=60)
    if response.status_code == 404:
        return None, url
    try:
        payload = json.loads(response.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _fail(f"{exchange} {url} 返回的不是 JSON，可能是错误页") from exc
    rows = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise _fail(f"{exchange} {url} 的 {key} 不是由对象组成的列表，格式可能已变")
    reported = payload.get("report_date")
    if reported is not None and _text(reported) != ymd:
        raise _fail(f"{exchange} 返回的 report_date={reported}，不是 {ymd}")
    if reported is None and not any(isinstance(v, list) and v for v in payload.values()):
        return None, url  # early files are empty shells without a report_date
    return payload, url


def _ine_ids(path: str, ymd: str, key: str, field: str) -> set[str]:
    """IDs INE published that day; used to strip INE products out of SHFE files."""
    payload, url = _shfe_payload("INE", path, ymd, key)
    if payload is None:
        if ymd >= _INE_FIRST_DAY[path]:
            raise _fail(
                f"上期能源 {ymd} 的对照文件缺失或为空（{url}），无法从上期所数据里剔除能源品种；"
                "可能尚未发布，稍后重试"
            )
        return set()
    ids = {_text(r.get(field)) for r in payload[key]}
    if not ids and ymd >= _INE_FIRST_DAY[path]:
        raise _fail(f"上期能源 {ymd} 的对照文件没有合约（{url}），无法剔除能源品种")
    return ids


def _shfe_futures(exchange: str, ymd: str) -> tuple[list[dict[str, Any]], str]:
    payload, url = _shfe_payload(exchange, "future/dailydata/kx", ymd, "o_curinstrument")
    if payload is None:
        raise _no_data(f"{exchange} {ymd} 没有数据：非交易日、尚未发布或该品种当时未上市")
    skip = _ine_ids("future/dailydata/kx", ymd, "o_curinstrument", "PRODUCTID") if exchange == "SHFE" else set()
    rows = []
    for row in payload["o_curinstrument"]:
        product_id = _text(_need(row, "PRODUCTID", f"{exchange} 日行情"))
        month = _text(row.get("DELIVERYMONTH"))
        # futures ids end in _f; sc_tas is a crude-oil TAS instruction, not a contract
        if not product_id.endswith("_f") or not month.isdigit() or product_id in skip:
            continue
        rows.append(
            {
                "symbol": product_id[:-2] + month,
                "product": _text(row.get("PRODUCTNAME")),
                "open": _price(row.get("OPENPRICE")),
                "high": _price(row.get("HIGHESTPRICE")),
                "low": _price(row.get("LOWESTPRICE")),
                "close": _price(row.get("CLOSEPRICE")),
                "settle": _price(row.get("SETTLEMENTPRICE")),
                "pre_settle": _price(row.get("PRESETTLEMENTPRICE")),
                "volume": to_int(row.get("VOLUME")),
                "open_interest": to_int(row.get("OPENINTEREST")),
                "oi_change": to_int(row.get("OPENINTERESTCHG")),
                "turnover_10k": to_float(row.get("TURNOVER")),
            }
        )
    return rows, url


def _shfe_options(exchange: str, ymd: str) -> tuple[list[dict[str, Any]], str]:
    payload, url = _shfe_payload(exchange, "option/dailydata/kx", ymd, "o_curinstrument")
    if payload is None:
        raise _no_data(f"{exchange} {ymd} 没有期权数据：非交易日或期权尚未上市")
    skip = _ine_ids("option/dailydata/kx", ymd, "o_curinstrument", "PRODUCTID") if exchange == "SHFE" else set()
    sigma_rows = payload.get("o_cursigma") or []
    if not isinstance(sigma_rows, list) or not all(isinstance(r, dict) for r in sigma_rows):
        raise _fail(f"{exchange} {url} 的 o_cursigma 不是由对象组成的列表")
    sigma: dict[str, float | None] = {}
    for row in sigma_rows:
        if "INSTRUMENTID" not in row:
            raise _fail(f"{exchange} {url} 的 o_cursigma 行没有 INSTRUMENTID")
        series_id = _text(row.get("INSTRUMENTID"))
        if series_id in ("小计", "合计", "总计"):
            continue
        if not series_id:
            raise _fail(f"{exchange} {url} 的 o_cursigma 有空的 INSTRUMENTID")
        if series_id in sigma:
            raise _fail(f"{exchange} {url} 的 o_cursigma 里 {series_id} 出现两次，隐含波动率会互相覆盖")
        sigma[series_id] = to_float(_need(row, "SIGMA", f"{exchange} {series_id} 的隐含波动率表"))

    rows = []
    for row in payload["o_curinstrument"]:
        code = _text(row.get("INSTRUMENTID"))
        kind = {"1": "C", "2": "P"}.get(_text(row.get("OPTIONSTYPE")))
        if kind is None or _text(row.get("PRODUCTID")) in skip:
            continue
        parsed = _OPTION_CODE.match(code)
        if not parsed or parsed.group(2) != kind:
            raise _fail(f"{exchange} 期权 {code} 的代码与 OPTIONSTYPE={row.get('OPTIONSTYPE')} 不一致")
        series = _text(_need(row, "UNDERLYINGINSTRID", f"{exchange} 期权"))
        if series not in sigma:
            raise _fail(f"{exchange} {url} 的 o_cursigma 里没有系列 {series}，结果不完整")
        iv = sigma[series]
        rows.append(
            {
                "symbol": code,
                "series": series,
                "option_type": kind,
                "strike": to_float(row.get("STRIKEPRICE")),
                "open": _price(row.get("OPENPRICE")),
                "high": _price(row.get("HIGHESTPRICE")),
                "low": _price(row.get("LOWESTPRICE")),
                "close": _price(row.get("CLOSEPRICE")),
                "settle": _price(row.get("SETTLEMENTPRICE")),
                "pre_settle": _price(row.get("PRESETTLEMENTPRICE")),
                "volume": to_int(row.get("VOLUME")),
                "open_interest": to_int(row.get("OPENINTEREST")),
                "oi_change": to_int(row.get("OPENINTERESTCHG")),
                "turnover_10k": to_float(row.get("TURNOVER")),
                "delta": to_float(row.get("DELTA")),
                "iv_pct": None,
                "series_iv_pct": round(iv * 100, 4) if iv is not None else None,
            }
        )
    return rows, url


# --------------------------------------------------------------------------- #
# CZCE (pipe-delimited text)
# --------------------------------------------------------------------------- #

def _czce_text(kind: str, name: str, ymd: str) -> tuple[str, str]:
    if ymd < _CZCE_FIRST_DAY:
        raise _no_data(f"郑商所数据从 {_CZCE_FIRST_DAY} 起接入（更早的文件是另一套格式）")
    url = _CZCE_URL.format(kind=kind, year=ymd[:4], ymd=ymd, name=name)
    response = fetch("czce", "GET", url, allow_status=(404,), timeout=60)
    if response.status_code == 404:
        raise _no_data(f"郑商所 {ymd} 没有 {name}：非交易日或尚未发布")
    try:
        text = response.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = response.content.decode("gbk")  # files up to 2017 are GBK
    stamp = f"({ymd[:4]}-{ymd[4:6]}-{ymd[6:]})"
    if stamp not in (text.split("\n", 1)[0] + text[:200]):
        raise _fail(f"郑商所 {name} 标题里的日期不是 {ymd}")
    return text, url


def _czce_rows(text: str, context: str) -> list[dict[str, Any]]:
    lines = [line for line in text.splitlines() if "|" in line]
    if not lines:
        raise _fail(f"郑商所 {context} 没有表格行，格式可能已变")
    header = [_CZCE_HEADER_ALIAS.get(c.strip(), c.strip()) for c in lines[0].split("|")]
    if not header or header[0] != "合约代码":
        raise _fail(f"郑商所 {context} 表头改变")
    rows = []
    for line in lines[1:]:
        cells = [c.strip().replace(",", "") for c in line.split("|")]
        if not cells or not cells[0] or cells[0].endswith(("小计", "总计", "合计")):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def _czce_futures(ymd: str) -> tuple[list[dict[str, Any]], str]:
    text, url = _czce_text("Future", "FutureDataDaily", ymd)
    rows = []
    for row in _czce_rows(text, "日行情"):
        code = _text(_need(row, "合约代码", "郑商所日行情"))
        rows.append(
            {
                "symbol": code,
                "product": _product_of(code),
                "open": _price(row.get("今开盘")),
                "high": _price(row.get("最高价")),
                "low": _price(row.get("最低价")),
                "close": _price(row.get("今收盘")),
                "settle": _price(row.get("今结算")),
                "pre_settle": _price(row.get("昨结算")),
                "volume": to_int(row.get("成交量(手)")),
                "open_interest": to_int(row.get("持仓量")),
                "oi_change": to_int(row.get("增减量")),
                "turnover_10k": to_float(row.get("成交额(万元)")),
            }
        )
    return rows, url


def _czce_options(ymd: str) -> tuple[list[dict[str, Any]], str]:
    text, url = _czce_text("Option", "OptionDataDaily", ymd)
    if "无交易记录" in text:
        raise _no_data(f"郑商所 {ymd} 没有期权成交记录（郑商所期权 2017-04-19 起上市）")
    rows = []
    for row in _czce_rows(text, "期权日行情"):
        code = _text(_need(row, "合约代码", "郑商所期权日行情"))
        parsed = _OPTION_CODE.match(code)
        if not parsed:
            raise _fail(f"郑商所期权代码无法解析: {code}")
        rows.append(
            {
                "symbol": code,
                "series": parsed.group(1),
                "option_type": parsed.group(2),
                "strike": to_float(parsed.group(3)),
                "open": _price(row.get("今开盘")),
                "high": _price(row.get("最高价")),
                "low": _price(row.get("最低价")),
                "close": _price(row.get("今收盘")),
                "settle": _price(row.get("今结算")),
                "pre_settle": _price(row.get("昨结算")),
                "volume": to_int(row.get("成交量(手)")),
                "open_interest": to_int(row.get("持仓量")),
                "oi_change": to_int(row.get("增减量")),
                "turnover_10k": to_float(row.get("成交额(万元)")),
                "delta": to_float(row.get("DELTA")),
                "iv_pct": to_float(row.get("隐含波动率")),
                "series_iv_pct": None,
            }
        )
    return rows, url


# --------------------------------------------------------------------------- #
# CFFEX (CSV + index.xml witness)
# --------------------------------------------------------------------------- #

def _cffex_csv(url: str) -> list[list[str]] | None:
    response = fetch("cffex", "GET", url, allow_status=(302, 404), allow_redirects=False, timeout=60)
    if response.status_code in (302, 404):
        return None
    try:
        text = response.content.decode("gbk")
    except UnicodeDecodeError as exc:
        raise _fail(f"中金所 {url} 不是 GBK 文本，格式可能已变") from exc
    return [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]


def _cffex_table(ymd: str) -> tuple[list[list[str]], str]:
    url = _CFFEX_DAILY.format(ym=ymd[:6], dd=ymd[6:], ymd=ymd)
    table = _cffex_csv(url)
    if table is None:
        raise _no_data(f"中金所 {ymd} 没有行情文件（非交易日或尚未发布）")
    header = [c.strip() for c in table[0]]
    if header[:3] != ["合约代码", "今开盘", "最高价"]:
        raise _fail("中金所日行情表头改变")
    _cffex_witness(table, ymd)
    return table, url


def _cffex_witness(table: list[list[str]], ymd: str) -> None:
    """The daily CSV has no date column; index.xml carries it per contract."""
    xml_url = _CFFEX_WITNESS.format(ym=ymd[:6], dd=ymd[6:])
    response = fetch("cffex", "GET", xml_url, allow_status=(302, 404), allow_redirects=False, timeout=60)
    if response.status_code in (302, 404):
        raise _fail(f"中金所 {ymd} 有行情 CSV 却没有 index.xml，无法核对交易日: {xml_url}")
    if b"<!DOCTYPE" in response.content or b"<!ENTITY" in response.content:
        raise _fail(f"中金所 index.xml 含 DOCTYPE / ENTITY，拒绝解析: {xml_url}")
    try:
        nodes = ElementTree.fromstring(response.content).findall("dailydata")
    except ElementTree.ParseError as exc:
        raise _fail(f"中金所 index.xml 无法解析: {exc}") from exc
    witness: dict[str, tuple[Any, ...]] = {}
    for node in nodes:
        values = {
            key: (node.findtext(key) or "").strip()
            for key in ("instrumentid", "tradingday", "volume", "closeprice", "openinterest")
        }
        if values["tradingday"] != ymd:
            raise _fail(f"中金所 index.xml 的交易日是 {values['tradingday']}，不是 {ymd}")
        witness[values["instrumentid"]] = tuple(
            to_float(values[key]) for key in ("volume", "closeprice", "openinterest")
        )
    header = [c.strip() for c in table[0]]
    parsed: dict[str, tuple[Any, ...]] = {}
    for record in table[1:]:
        code = record[0].strip() if record else ""
        if not code or code in ("小计", "合计", "总计"):
            continue
        row = dict(zip(header, record))
        parsed[code] = tuple(to_float(row.get(key)) for key in ("成交量", "今收盘", "持仓量"))
    if not witness or not parsed or parsed != witness:
        diff = sorted(set(parsed) ^ set(witness)) or sorted(
            key for key in parsed if parsed[key] != witness.get(key)
        )
        raise _fail(
            f"中金所 {ymd} 行情 CSV 与 index.xml 对不上（{len(parsed)} / {len(witness)} 个合约，"
            f"例如 {diff[:3]}），不能确认 CSV 属于这一天"
        )


def _cffex_futures(ymd: str) -> tuple[list[dict[str, Any]], str]:
    table, url = _cffex_table(ymd)
    header = [c.strip() for c in table[0]]
    rows = []
    for record in table[1:]:
        code = record[0].strip() if record else ""
        if not code or code in ("小计", "合计", "总计") or "-C-" in code or "-P-" in code:
            continue
        row = dict(zip(header, record))
        rows.append(
            {
                "symbol": code,
                "product": _product_of(code),
                "open": _price(row.get("今开盘")),
                "high": _price(row.get("最高价")),
                "low": _price(row.get("最低价")),
                "close": _price(row.get("今收盘")),
                "settle": _price(row.get("今结算")),
                "pre_settle": _price(row.get("前结算")),
                "volume": to_int(row.get("成交量")),
                "open_interest": to_int(row.get("持仓量")),
                "oi_change": to_int(row.get("持仓变化")),
                "turnover_10k": to_float(row.get("成交金额")),
            }
        )
    return rows, url


def _cffex_options(ymd: str) -> tuple[list[dict[str, Any]], str]:
    table, url = _cffex_table(ymd)
    header = [c.strip() for c in table[0]]
    rows = []
    for record in table[1:]:
        code = record[0].strip() if record else ""
        if "-C-" not in code and "-P-" not in code:
            continue
        parsed = _OPTION_CODE.match(code)
        if not parsed:
            raise _fail(f"中金所期权代码无法解析: {code}")
        row = dict(zip(header, record))
        rows.append(
            {
                "symbol": code,
                "series": parsed.group(1),
                "option_type": parsed.group(2),
                "strike": to_float(parsed.group(3)),
                "open": _price(row.get("今开盘")),
                "high": _price(row.get("最高价")),
                "low": _price(row.get("最低价")),
                "close": _price(row.get("今收盘")),
                "settle": _price(row.get("今结算")),
                "pre_settle": _price(row.get("前结算")),
                "volume": to_int(row.get("成交量")),
                "open_interest": to_int(row.get("持仓量")),
                "oi_change": to_int(row.get("持仓变化")),
                "turnover_10k": to_float(row.get("成交金额")),
                "delta": None,
                "iv_pct": None,
                "series_iv_pct": None,
            }
        )
    return rows, url


# --------------------------------------------------------------------------- #
# GFEX (JSON POST; HTTPS presents a foreign certificate)
# --------------------------------------------------------------------------- #

def _gfex_rows(ymd: str, trade_type: int) -> tuple[list[dict[str, Any]], str]:
    response = fetch(
        "gfex",
        "POST",
        _GFEX_URL,
        data={"trade_date": ymd, "trade_type": trade_type},
        headers={"Referer": _GFEX_REFERER},
        timeout=60,
    )
    try:
        payload = json.loads(response.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _fail("广期所返回的不是 JSON，格式可能已变") from exc
    if not isinstance(payload, dict) or _text(payload.get("code")) != "0":
        raise _fail(f"广期所返回错误: {str(payload)[:200]}")
    param = payload.get("param")
    if not isinstance(param, dict):
        raise _fail(f"广期所没有回显请求参数（param 应为对象）: {str(payload)[:200]}")
    echoed_date = param.get("trade_date")
    echoed_type = param.get("trade_type")
    if ymd not in (echoed_date if isinstance(echoed_date, list) else [echoed_date]):
        raise _fail(f"广期所回显的交易日 {echoed_date} 与请求的 {ymd} 不符")
    if str(trade_type) not in [str(v) for v in (echoed_type if isinstance(echoed_type, list) else [echoed_type])]:
        raise _fail(f"广期所回显的 trade_type {echoed_type} 与请求的 {trade_type} 不符")
    data = payload.get("data")
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise _fail("广期所 data 不是由对象组成的列表，格式可能已变")
    rows = []
    for row in data:
        if _text(row.get("variety")).endswith(("小计", "总计")):
            continue
        if not _text(row.get("delivMonth")):
            raise _fail(f"广期所合约行缺 delivMonth，格式可能已变: {str(row)[:120]}")
        rows.append(row)
    if not rows:
        raise _no_data(f"广期所 {ymd} 没有行情：非交易日或尚未发布")
    return rows, response.url


def _gfex_futures(ymd: str) -> tuple[list[dict[str, Any]], str]:
    data, url = _gfex_rows(ymd, 0)
    rows = []
    for row in data:
        rows.append(
            {
                "symbol": _text(row.get("varietyOrder")) + _text(row.get("delivMonth")),
                "product": _text(row.get("variety")),
                "open": _price(row.get("open")),
                "high": _price(row.get("high")),
                "low": _price(row.get("low")),
                "close": _price(row.get("close")),
                "settle": _price(row.get("clearPrice")),
                "pre_settle": _price(row.get("lastClear")),
                "volume": to_int(row.get("volumn")),
                "open_interest": to_int(row.get("openInterest")),
                "oi_change": to_int(row.get("diffI")),
                "turnover_10k": to_float(row.get("turnover")),
            }
        )
    return rows, url


def _gfex_options(ymd: str) -> tuple[list[dict[str, Any]], str]:
    data, url = _gfex_rows(ymd, 1)
    rows = []
    for row in data:
        code = _text(row.get("delivMonth"))
        parsed = _OPTION_CODE.match(code)
        if not parsed:
            raise _fail(f"广期所期权代码无法解析: {code}")
        rows.append(
            {
                "symbol": code,
                "series": parsed.group(1),
                "option_type": parsed.group(2),
                "strike": to_float(parsed.group(3)),
                "open": _price(row.get("open")),
                "high": _price(row.get("high")),
                "low": _price(row.get("low")),
                "close": _price(row.get("close")),
                "settle": _price(row.get("clearPrice")),
                "pre_settle": _price(row.get("lastClear")),
                "volume": to_int(row.get("volumn")),
                "open_interest": to_int(row.get("openInterest")),
                "oi_change": to_int(row.get("diffI")),
                "turnover_10k": to_float(row.get("turnover")),
                "delta": to_float(row.get("delta")),
                "iv_pct": to_float(row.get("impliedVolatility")),
                "series_iv_pct": None,
            }
        )
    return rows, url


def _fetch_daily(
    exchanges: list[str],
    loader: Any,
    ymd: str,
    columns: list[str],
    day: str,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    urls: list[str] = []
    failures: list[dict[str, Any]] = []
    first_error: TickHubError | None = None
    for exchange in exchanges:
        try:
            part, url = loader(exchange, ymd)
        except TickHubError as exc:
            if exchange == exchanges[0]:
                first_error = exc
            failures.append({"exchange": exchange, "error": exc.to_dict()})
            continue
        if not part:
            failures.append(
                {
                    "exchange": exchange,
                    "error": {"code": "QFSD-9003", "message": f"{exchange} {day} 解析出 0 个合约，格式可能已变"},
                }
            )
            continue
        urls.append(url)
        for row in part:
            row["date"] = day
            row["exchange"] = exchange
        rows.extend(part)
        seen: set[str] = set()
        for row in part:
            symbol = row["symbol"]
            if symbol in seen:
                raise _fail(f"{exchange} {day} 合约代码重复: {symbol}")
            seen.add(symbol)
    if not rows:
        if first_error and len(failures) == len(exchanges):
            raise first_error
        raise _no_data(f"{day} 没有数据：非交易日、尚未发布或品种当时未上市")
    source = _source(exchanges[0]) if len(exchanges) == 1 else "exchange"
    extra: dict[str, Any] = {"source_url": " | ".join(urls), "columns": columns}
    if failures:
        extra["partial"] = True
        extra["failures"] = failures
    return frame(rows, source, **extra)


# --------------------------------------------------------------------------- #
# Public tools
# --------------------------------------------------------------------------- #

@tool(layer="futures", ttl="static", cache_domain="fut")
def futures_daily(date: str = "", exchange: str = "SHFE") -> dict[str, Any]:
    """Futures daily close from the exchange itself (SHFE/INE/CZCE/CFFEX/GFEX, or ALL).

    ``settle`` is the official settlement price, ``turnover_10k`` is in CNY 10k.
    Products with no trade report price 0 upstream; those are normalised to null.
    SHFE files also contain INE products (crude oil, NR...), which are removed
    using INE's file for the same day so SHFE and INE never overlap.
    """
    day, ymd = _day(date)
    name = _text(exchange).upper() or "SHFE"
    if name == "ALL":
        exchanges = list(EXCHANGES)
    else:
        exchanges = [_check_exchange(name)]

    def loader(item: str, stamp: str) -> tuple[list[dict[str, Any]], str]:
        if item in ("SHFE", "INE"):
            return _shfe_futures(item, stamp)
        if item == "CZCE":
            return _czce_futures(stamp)
        if item == "CFFEX":
            return _cffex_futures(stamp)
        return _gfex_futures(stamp)

    return _fetch_daily(exchanges, loader, ymd, _FUT_COLUMNS, day)


@tool(layer="futures", ttl="static", cache_domain="fut")
def options_daily(date: str = "", exchange: str = "SHFE") -> dict[str, Any]:
    """Commodity / index option daily close from the exchange itself.

    ``series`` is the underlying futures contract (CFFEX: product + month).
    ``delta`` is published by SHFE/INE/CZCE/GFEX, never by CFFEX. ``iv_pct`` is
    per contract (CZCE/GFEX); ``series_iv_pct`` is SHFE/INE's per-series SIGMA.
    ETF options live in the ETF option layer, not here.
    """
    day, ymd = _day(date)
    name = _text(exchange).upper() or "SHFE"
    if name == "ALL":
        exchanges = list(EXCHANGES)
    else:
        exchanges = [_check_exchange(name)]

    def loader(item: str, stamp: str) -> tuple[list[dict[str, Any]], str]:
        if item in ("SHFE", "INE"):
            return _shfe_options(item, stamp)
        if item == "CZCE":
            return _czce_options(stamp)
        if item == "CFFEX":
            return _cffex_options(stamp)
        return _gfex_options(stamp)

    return _fetch_daily(exchanges, loader, ymd, _OPT_COLUMNS, day)


def _rank_member(value: Any) -> str | None:
    text = _text(value)
    return text if text and text != "-" else None


@tool(layer="futures", ttl="static", cache_domain="fut")
def futures_position_rank(date: str = "", exchange: str = "SHFE", symbol: str = "") -> dict[str, Any]:
    """Top-20 member volume / long / short positions from the exchange.

    ``level='contract'`` is one contract; ``level='product'`` (CZCE only) is the
    product aggregate. SHFE/INE rows without a 1-20 rank are member-type
    summaries and are dropped. CFFEX publishes one file per product; every
    listed product missing that day makes the result fail rather than partial.
    GFEX and DCE are not covered.
    """
    day, ymd = _day(date)
    name = _check_exchange(exchange)
    if name == "GFEX":
        raise TickHubError("QFSD-9002", "广期所持仓排名未接入")
    rows: list[dict[str, Any]] = []
    urls: list[str] = []
    if name in ("SHFE", "INE"):
        payload, url = _shfe_payload(name, "future/dailydata/pm", ymd, "o_cursor")
        if payload is None:
            raise _no_data(f"{name} {ymd} 没有持仓排名：非交易日或尚未发布")
        skip = _ine_ids("future/dailydata/pm", ymd, "o_cursor", "INSTRUMENTID") if name == "SHFE" else set()
        urls.append(url)
        for row in payload["o_cursor"]:
            code = _text(_need(row, "INSTRUMENTID", f"{name} 持仓排名"))
            rank = to_int(row.get("RANK"))
            if rank is None:
                raise _fail(f"{name} {code} 持仓排名缺名次字段")
            if not 1 <= rank <= 20 or code in skip:
                continue
            rows.append(
                {
                    "level": "contract",
                    "symbol": code,
                    "rank": rank,
                    "volume_member": _rank_member(row.get("PARTICIPANTABBR1")),
                    "volume": to_int(row.get("CJ1")),
                    "volume_chg": to_int(row.get("CJ1_CHG")),
                    "long_member": _rank_member(row.get("PARTICIPANTABBR2")),
                    "long_oi": to_int(row.get("CJ2")),
                    "long_chg": to_int(row.get("CJ2_CHG")),
                    "short_member": _rank_member(row.get("PARTICIPANTABBR3")),
                    "short_oi": to_int(row.get("CJ3")),
                    "short_chg": to_int(row.get("CJ3_CHG")),
                }
            )
    elif name == "CZCE":
        text, url = _czce_text("Future", "FutureDataHolding", ymd)
        urls.append(url)
        level: str | None = None
        code: str | None = None
        for line in text.splitlines():
            head = re.match(r"^(品种|合约)：\s*(\S+)\s+日期：", line)
            if head:
                level = "product" if head.group(1) == "品种" else "contract"
                found = re.search(r"[A-Za-z]+\d*$", head.group(2))
                if not found:
                    raise _fail(f"郑商所持仓排名表头认不出品种 / 合约: {line[:60]}")
                code = found.group(0)
                continue
            cells = [c.strip().replace(",", "") for c in line.split("|")]
            if len(cells) < 10 or not cells[0].isdigit():
                continue
            if code is None or level is None:
                raise _fail("郑商所持仓排名在品种/合约标题之前出现数据行")
            rows.append(
                {
                    "level": level,
                    "symbol": code,
                    "rank": int(cells[0]),
                    "volume_member": _rank_member(cells[1]),
                    "volume": to_int(cells[2]),
                    "volume_chg": to_int(cells[3]),
                    "long_member": _rank_member(cells[4]),
                    "long_oi": to_int(cells[5]),
                    "long_chg": to_int(cells[6]),
                    "short_member": _rank_member(cells[7]),
                    "short_oi": to_int(cells[8]),
                    "short_chg": to_int(cells[9]),
                }
            )
    else:
        expected = [product for product, first in _CFFEX_RANK_FIRST_DAY.items() if ymd >= first]
        if not expected:
            raise _no_data("中金所持仓排名从 2010-04-16（沪深300 期货上市）起才有")
        missing: list[str] = []
        for product in expected:
            url = _CFFEX_RANK.format(ym=ymd[:6], dd=ymd[6:], product=product)
            table = _cffex_csv(url)
            if table is None:
                missing.append(product)
                continue
            urls.append(url)
            before = len(rows)
            rows.extend(_cffex_rank_rows(table, product, ymd))
            if len(rows) == before:
                raise _fail(f"中金所 {product} {day} 持仓排名文件解析出 0 行，格式可能已变")
        if len(missing) == len(expected):
            raise _no_data(f"中金所 {day} 没有持仓排名：非交易日或尚未发布")
        if missing:
            raise _fail(
                f"中金所 {day} 缺少已上市品种 {'/'.join(missing)} 的持仓排名，结果不完整（可能尚未全部发布，稍后重试）"
            )
    if not rows:
        raise _fail(f"{name} {day} 持仓排名解析出 0 行，格式可能已变")
    seen: set[tuple[str, str, int]] = set()
    for row in rows:
        row["date"], row["exchange"] = day, name
        key = (row["level"], row["symbol"], row["rank"])
        if key in seen:
            raise _fail(f"{name} {day} 持仓排名 合约+名次 重复: {key}")
        seen.add(key)
    if symbol:
        wanted = _text(symbol).upper()
        rows = [row for row in rows if row["symbol"].upper() == wanted]
    return frame(rows, _source(name), source_url=" | ".join(urls), columns=_RANK_COLUMNS)


_CFFEX_RANK_SUB = [
    "会员简称", "成交量", "比上一交易日增减",
    "会员简称", "持买单量", "比上一交易日增减",
    "会员简称", "持卖单量", "比上一交易日增减",
]


def _cffex_rank_rows(table: list[list[str]], product: str, ymd: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    in_rank = False
    index = 0
    while index < len(table):
        cells = [c.strip() for c in table[index]]
        if cells[:2] == ["交易日", "合约"]:
            in_rank = cells[2:3] == ["排名"]
            if in_rank:
                sub = [c.strip() for c in table[index + 1][3:12]] if index + 1 < len(table) else []
                if cells[3:12:3] != ["成交量排名", "持买单量排名", "持卖单量排名"] or sub != _CFFEX_RANK_SUB:
                    raise _fail(f"中金所 {product} 持仓排名表头变了: {cells} / {sub}")
                index += 1
        elif cells[2:3] and cells[2].isdigit():
            if not in_rank:
                raise _fail(f"中金所 {product} 持仓排名在排名表头之前出现数据行: {cells}")
            if len(cells) < 12:
                raise _fail(f"中金所 {product} 持仓排名行缺列: {cells}")
            if cells[0] != ymd:
                raise _fail(f"中金所 {product} 持仓排名交易日是 {cells[0]}，不是 {ymd}")
            rows.append(
                {
                    "level": "contract",
                    "symbol": cells[1],
                    "rank": int(cells[2]),
                    "volume_member": _rank_member(cells[3]),
                    "volume": to_int(cells[4]),
                    "volume_chg": to_int(cells[5]),
                    "long_member": _rank_member(cells[6]),
                    "long_oi": to_int(cells[7]),
                    "long_chg": to_int(cells[8]),
                    "short_member": _rank_member(cells[9]),
                    "short_oi": to_int(cells[10]),
                    "short_chg": to_int(cells[11]),
                }
            )
        index += 1
    return rows


# --------------------------------------------------------------------------- #
# Sina realtime / daily kline / A50
# --------------------------------------------------------------------------- #

def _sina_quotes(codes: list[str]) -> tuple[dict[str, list[str]], str]:
    url = _SINA_HQ + ",".join(codes)
    response = fetch(
        "sina", "GET", url, headers={"Referer": "https://finance.sina.com.cn/"}, timeout=20
    )
    text = response.content.decode("gbk", "replace")
    quotes: dict[str, list[str]] = {}
    for key, body in re.findall(r'var hq_str_([^=]+)="([^"]*)"', text):
        quotes[key] = body.split(",") if body else []
    if not quotes:
        raise _fail(f"新浪行情页没有 hq_str 变量（{response.url}），格式可能已变")
    return quotes, response.url


def _normalize_future_symbols(symbols: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(symbols, str):
        raw_items: list[str] = re.split(r"[,;\s]+", symbols)
    else:
        raw_items = [str(item) for item in symbols]
    codes = []
    for raw in raw_items:
        code = raw.strip()
        if not code:
            continue
        if code.lower().startswith("nf_"):
            code = code[3:]
        if not _FUT_SYMBOL.match(code):
            raise _bad_arg(f"期货代码格式不对: {raw}（例 RB0 / CU2610 / IF2609）")
        codes.append(code.upper())
    if not codes:
        raise _bad_arg("symbols 需为非空的代码或代码列表（例 'RB0' / ['RB0', 'IF2609']）")
    return codes


@tool(layer="futures", ttl="realtime", cache_domain="fut")
def futures_realtime(symbols: str = "RB0") -> dict[str, Any]:
    """Domestic futures realtime quote (Sina) — the only source covering DCE.

    Symbols: ``RB0`` (main continuous), ``CU2610``, ``IF2609``, ``M0``. CFFEX
    products also carry ``pre_close`` and price limits. After the close this is
    the day's closing snapshot; settlement prices only come from
    ``futures_daily``.
    """
    codes = _normalize_future_symbols(symbols)
    quotes, url = _sina_quotes(["nf_" + code for code in codes])
    rows = []
    for code in codes:
        fields = quotes.get("nf_" + code)
        if not fields:
            raise _no_data(f"新浪没有期货 {code} 的行情（代码不存在或已摘牌）")
        if _product_of(code) in _CFFEX_SINA_PRODUCTS:
            if len(fields) < 50:
                raise _fail(f"新浪中金所期货 {code} 字段数 {len(fields)}，格式可能已变")
            clock = f"{fields[37]}" if ":" in fields[37] else fields[37]
            rows.append(
                {
                    "symbol": code,
                    "name": fields[49],
                    "datetime": f"{fields[36]} {clock}",
                    "open": _price(fields[0]),
                    "high": _price(fields[1]),
                    "low": _price(fields[2]),
                    "last": _price(fields[3]),
                    "bid": _price(fields[16]),
                    "ask": _price(fields[26]),
                    "bid_vol": to_int(fields[17]),
                    "ask_vol": to_int(fields[27]),
                    "volume": to_int(fields[4]),
                    "open_interest": to_int(fields[6]),
                    "pre_settle": _price(fields[14]),
                    "pre_close": _price(fields[13]),
                    "upper_limit": _price(fields[9]),
                    "lower_limit": _price(fields[10]),
                    "avg_price": _price(fields[48]),
                }
            )
        else:
            if len(fields) < 28:
                raise _fail(f"新浪商品期货 {code} 字段数 {len(fields)}，格式可能已变")
            clock = (fields[1] or "").zfill(6)
            rows.append(
                {
                    "symbol": code,
                    "name": fields[0],
                    "datetime": f"{fields[17]} {clock[:2]}:{clock[2:4]}:{clock[4:]}",
                    "open": _price(fields[2]),
                    "high": _price(fields[3]),
                    "low": _price(fields[4]),
                    "last": _price(fields[8]),
                    "bid": _price(fields[6]),
                    "ask": _price(fields[7]),
                    "bid_vol": to_int(fields[11]),
                    "ask_vol": to_int(fields[12]),
                    "volume": to_int(fields[14]),
                    "open_interest": to_int(fields[13]),
                    "pre_settle": _price(fields[10]),
                    "pre_close": None,
                    "upper_limit": None,
                    "lower_limit": None,
                    "avg_price": _price(fields[27]),
                }
            )
    return frame(rows, "sina", source_url=url)


@tool(layer="futures", ttl="semi_static", cache_domain="fut")
def futures_kline(symbol: str = "RB0", start: str = "", end: str = "") -> dict[str, Any]:
    """Daily futures K-line from Sina — one contract or a main continuous.

    Covers all six exchanges including DCE. Main-continuous series are not
    back-adjusted, so roll days show a gap. Sina returns the full history on
    every call; ``start`` / ``end`` filter locally. Settlement prices are often
    missing (0 upstream -> null); use ``futures_daily`` when they matter.
    """
    code = _text(symbol)
    if code.lower().startswith("nf_"):
        code = code[3:]
    if not _FUT_SYMBOL.match(code):
        raise _bad_arg(f"期货代码格式不对: {symbol}（例 RB0 / RB2601 / MA2601）")
    code = code.upper()
    try:
        lo = parse_date(start) if start else None
        hi = parse_date(end) if end else None
    except ValueError as exc:
        raise _bad_arg(str(exc)) from exc
    if lo and hi and lo > hi:
        raise _bad_arg(f"start {lo} 晚于 end {hi}")
    url = _SINA_KLINE.format(code=code)
    response = fetch(
        "sina",
        "GET",
        url,
        params={"symbol": code},
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=25,
    )
    text = response.content.decode("gbk", "replace")
    match = re.search(rf"var _{re.escape(code)}=\((.*)\);?\s*$", text, re.S)
    if not match:
        raise _fail(f"新浪期货日 K {code} 的返回不是预期的 JSONP（{response.url}），格式可能已变")
    body = match.group(1).strip()
    if body == "null":
        raise _no_data(
            f"新浪没有期货 {code} 的日 K：代码不存在，或是约 2022 年以前到期的老合约"
            "（郑商所也要写 4 位年月，如 MA2601）"
        )
    try:
        items = json.loads(body)
    except ValueError as exc:
        raise _fail(f"新浪期货日 K {code} 的返回不是 JSON，格式可能已变") from exc
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise _fail(f"新浪期货日 K {code} 的行不是对象列表，格式可能已变")
    rows = []
    seen: set[str] = set()
    for item in items:
        day = _text(_need(item, "d", f"新浪期货日 K {code}"))
        if day in seen:
            raise _fail(f"新浪期货日 K {code} 同一天 {day} 出现两次，结果不可信")
        seen.add(day)
        if (lo and day < lo) or (hi and day > hi):
            continue
        rows.append(
            {
                "date": day,
                "symbol": code,
                "open": _price(item.get("o")),
                "high": _price(item.get("h")),
                "low": _price(item.get("l")),
                "close": _price(item.get("c")),
                "settle": _price(item.get("s")),
                "volume": to_int(item.get("v")),
                "open_interest": to_int(item.get("p")),
            }
        )
    if not rows:
        raise _no_data(f"新浪期货 {code} 在所给区间内没有日 K（合约当时未上市或已到期）")
    rows.sort(key=lambda row: row["date"])
    return frame(rows, "sina", source_url=response.url, columns=_KLINE_COLUMNS)


@tool(layer="futures", ttl="realtime", cache_domain="fut")
def a50_futures() -> dict[str, Any]:
    """FTSE China A50 index futures (Sina ``hf_CHA50CFD``) — offshore A-share gauge."""
    quotes, url = _sina_quotes(["hf_CHA50CFD"])
    fields = quotes.get("hf_CHA50CFD")
    if not fields or len(fields) < 14:
        raise _fail("新浪 A50 期指行情为空或字段数不对")
    row = {
        "symbol": "CHA50CFD",
        "name": fields[13],
        "datetime": f"{fields[12]} {fields[6]}",
        "last": _price(fields[0]),
        "open": _price(fields[8]),
        "high": _price(fields[4]),
        "low": _price(fields[5]),
        "bid": _price(fields[2]),
        "ask": _price(fields[3]),
        "pre_settle": _price(fields[7]),
    }
    if row["last"] is None:
        raise _fail("新浪 A50 期指最新价为空")
    return frame([row], "sina", source_url=url)


# --------------------------------------------------------------------------- #
# Shanghai Gold Exchange spot
# --------------------------------------------------------------------------- #

@tool(layer="futures", ttl="semi_static", cache_domain="fut")
def sge_spot(instrument: str = "Au99.99") -> dict[str, Any]:
    """SGE spot daily bars (official) — gold in CNY/g, silver in CNY/kg.

    Instruments: ``Au99.99`` / ``Au(T+D)`` / ``mAu(T+D)`` / ``Ag(T+D)`` /
    ``Ag99.99`` / ``Pt99.95``. Days with no trade are reported as all-zero rows
    and dropped. A handful of days in the official data have a close slightly
    outside the high-low range; those dates are listed in ``meta.ohlc_anomaly_dates``.
    """
    name = _text(instrument) or "Au99.99"
    response = fetch(
        "sge",
        "POST",
        _SGE_URL,
        data={"instid": name},
        headers={"Referer": "https://www.sge.com.cn/"},
        timeout=30,
    )
    try:
        payload = json.loads(response.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _fail("上金所日线返回的不是 JSON，格式可能已变") from exc
    series = payload.get("time") if isinstance(payload, dict) else None
    if not isinstance(series, list):
        raise _fail("上金所日线返回结构改变")
    rows = []
    for record in series:
        if not isinstance(record, list) or len(record) != 5:
            raise _fail(f"上金所日线字段数不对: {record}")
        prices = [to_float(value) for value in record[1:]]
        if None in prices:
            raise _fail(f"上金所日线出现空值或非有限数值: {record}")
        if not all(prices):
            continue
        open_, close, low, high = prices
        rows.append(
            {
                "date": _text(record[0]),
                "instrument": name,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
            }
        )
    if not rows:
        raise _no_data(f"上金所没有 {name} 的有效行情（代码不存在时返回全 0）")
    bad = [
        row["date"]
        for row in rows
        if not row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]
    ]
    if len(bad) > 0.05 * len(rows):
        raise _fail(f"上金所日线 {len(bad)}/{len(rows)} 天高低开收关系不成立，字段顺序可能已变")
    return frame(rows, "sge", source_url=response.url, ohlc_anomaly_dates=bad)


__all__ = [
    "EXCHANGES",
    "a50_futures",
    "futures_daily",
    "futures_kline",
    "futures_position_rank",
    "futures_realtime",
    "options_daily",
    "sge_spot",
]
