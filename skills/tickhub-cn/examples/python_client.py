"""Call TickHub-CN in-process: no server, no subprocess, same envelopes.

Run with ``python skills/tickhub-cn/examples/python_client.py 600519`` after
``pip install -e .``. Every number printed here comes from a tool result.
"""

from __future__ import annotations

import sys

from tickhub_cn import StockDataClient
from tickhub_cn.core.errors import TickHubError


def describe(label: str, payload: dict) -> None:
    meta = payload.get("meta") or {}
    print(
        f"{label}: source={meta.get('source')} "
        f"confidence={meta.get('confidence')} "
        f"cache_hit={meta.get('cache_hit')} "
        f"trace_id={meta.get('trace_id')}"
    )


def main(argv: list[str]) -> int:
    code = argv[1] if len(argv) > 1 else "600000"
    client = StockDataClient()

    try:
        quote = client.tencent_quote(code)
        if not quote["data"]:
            note = (quote.get("meta") or {}).get("note", "no record")
            print(f"{code}: no quote ({note})")
            return 1
        row = quote["data"][0]
        print(f"{row['code']} {row['name']} last={row['price']} pe={row['pe']} pb={row['pb']}")
        describe("quote", quote)

        bars = client.tencent_kline(code, period="daily", adjust="qfq", limit=5)
        klines = bars["data"]["klines"]
        if klines:
            first, last = klines[0], klines[-1]
            print(f"kline {first['date']}..{last['date']} close={last['close']} ({len(klines)} bars)")
        describe("kline", bars)

        check = client.finance_crosscheck(code)
        result = check["data"]["crosscheck"]
        print(f"crosscheck {result['status']} report_date={check['data']['report_date']}")
        for rule in result["rules"]:
            detail = rule.get("diff") if rule["status"] != "skip" else rule.get("reason")
            print(f"  {rule['rule']}: {rule['status']} ({detail})")
        describe("crosscheck", check)

        projected = client.tencent_quote(code, fields="code,name,price")
        print(f"projected keys: {sorted(projected['data'][0])}")
    except TickHubError as exc:
        print(f"{exc.code}: {exc.message} (exit {exc.exit_code}, retryable={exc.retryable})")
        return exc.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
