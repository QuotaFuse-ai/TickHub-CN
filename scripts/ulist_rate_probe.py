"""单路径限速探针：东财 ``push2`` ``ulist.np`` 的安全请求间隔（QUOT-407）。

2026-11-05 老板口径：当前阶段用 **15 秒档**（15 / 30 / 60），次数 = 600 秒 ÷ 间隔、
时长一律 ≥10 分钟；秒级档（5s / 1s）保留在 ``SECOND_LEVEL_STAGES`` 里，等商业代理池
上线再启用（启用时次数回到 ≥60）。

两种模式：

- ``rate``（默认）：遇到风控信号立即中止并回落到上一档，用于求安全速率；
- ``census``：已知出口被上游拒绝时，按授权把计划数采满，失败逐条记因，不做速率判定。

刻意不 import ``tickhub_cn``：探针要测的就是生产闸门（25s / 3 次每 60s）**以下**的速率，
走 ``fetch`` 会先被闸门拦下；同时避免在进程内 trip 真实的 ``ENDPOINT_BREAKER``。
本脚本不改任何闸门常量，只发请求、记账、下结论。

示例::

    # 先看计划，不发请求
    python scripts/ulist_rate_probe.py --codes-file pool.txt --dry-run

    # 用专用出口跑完整阶梯（单进程、顺序、无并发）
    python scripts/ulist_rate_probe.py --codes-file pool.txt \\
        --proxy http://10.0.0.9:3128 \\
        --json-out logs/ulist_probe.json --md-out logs/ulist_probe.md

中止条件按授权执行：任一 429/403、任一 5xx、任一连接重置、连续 2 次失败、
连续 2 次行数不足、非 JSON 响应、p95 连续 3 个窗口 >3× 基线，任一触发即停。
"""

from __future__ import annotations

import argparse
import errno
import http.client
import json
import math
import socket
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
# 与 datasources/funds.py::_ULIST_FIELDS 一致：批量只取主力两列。
FIELDS = "f12,f14,f2,f3,f62,f184"
EXPECTED_ROWS = 150
CST = timezone(timedelta(hours=8))

# 授权阶梯：秒/请求。秒级档（5 / 1）保留在代码里，等商业代理池上线再启用。
STAGES = (15.0, 30.0, 60.0)
SECOND_LEVEL_STAGES = (5.0, 1.0)
DEFAULT_MIN_SECONDS = 600
# 次数由档位换算：600 秒 ÷ 间隔（15s→40、30s→20、60s→10）；秒级档自然 ≥60。
DEFAULT_MIN_REQUESTS = 0
DEFAULT_COOLDOWN = 600.0
SAFETY_FACTOR = 2.0
FLOOR_INTERVAL = 10.0
MODES = ("rate", "census")

_RESET_ERRNOS = {errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE}
_RESET_WINERRNOS = {10053, 10054, 10058}
_RESET_NAMES = {
    "ConnectionResetError",
    "ConnectionAbortedError",
    "BrokenPipeError",
    "RemoteDisconnected",
}


def _exception_chain(exc: BaseException) -> Iterable[BaseException]:
    """跟着 ``reason`` / ``__cause__`` / ``__context__`` 把包装层剥开。"""
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        stack.extend(
            getattr(current, attr, None) for attr in ("reason", "__cause__", "__context__")
        )


def is_reset(exc: BaseException) -> bool:
    """按异常类型 + errno/winerror 判定连接重置，不依赖消息语言。

    中文 Windows 上 ``ConnectionResetError(10054)`` 的文本是中文，生产里那套英文
    marker 匹配会漏判（见 docs/decisions/QUOT-407-endpoint-breaker-semantics.md §6）。
    """
    for item in _exception_chain(exc):
        if type(item).__name__ in _RESET_NAMES:
            return True
        if getattr(item, "winerror", None) in _RESET_WINERRNOS:
            return True
        if getattr(item, "errno", None) in _RESET_ERRNOS:
            return True
    return False


def _classify(exc: BaseException) -> str:
    if is_reset(exc):
        return "reset"
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (429, 403):
            return "blocked"
        if exc.code >= 500:
            return "server_error"
        return "http_error"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        return "transport"
    if isinstance(exc, http.client.HTTPException):
        return "transport"
    return "transport"


@dataclass
class Sample:
    index: int
    at: str
    kind: str
    rt_ms: float
    http_status: int | None = None
    rows: int | None = None
    error: str | None = None
    errno: int | None = None
    winerror: int | None = None
    body: str | None = None


@dataclass
class Stage:
    interval: float
    planned: int
    seconds: float
    expected_rows: int = EXPECTED_ROWS
    samples: list[Sample] = field(default_factory=list)
    aborted: str | None = None
    abort_at: int | None = None
    slow_windows: int = 0

    @property
    def ok(self) -> int:
        return sum(1 for s in self.samples if s.kind == "ok")

    def kind_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.samples:
            out[s.kind] = out.get(s.kind, 0) + 1
        return out

    def latencies(self) -> list[float]:
        return [s.rt_ms for s in self.samples if s.kind == "ok"]

    def summary(self) -> dict[str, Any]:
        lat = self.latencies()
        counts = self.kind_counts()
        return {
            "interval_s": self.interval,
            "planned_requests": self.planned,
            "planned_seconds": round(self.seconds, 1),
            "attempts": len(self.samples),
            "ok": self.ok,
            "fail": len(self.samples) - self.ok,
            "kinds": counts,
            "reset": counts.get("reset", 0),
            "blocked": counts.get("blocked", 0),
            "server_error": counts.get("server_error", 0),
            "row_short": sum(
                1 for s in self.samples if s.rows is not None and s.rows < self.expected_rows
            ),
            "p50_ms": round(statistics.median(lat), 1) if lat else None,
            "p95_ms": round(_percentile(lat, 95), 1) if lat else None,
            "max_ms": round(max(lat), 1) if lat else None,
            "aborted": self.aborted,
            "abort_at": self.abort_at,
        }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def plan_stage(interval: float, min_seconds: int, min_requests: int) -> tuple[int, float]:
    """返回 (请求数, 实际时长秒)。

    次数按档位换算：``min_seconds ÷ interval``（15s→40、30s→20、60s→10）；
    ``min_requests`` 只作下限覆盖。首末样本之间只有 ``(count-1) × interval``，
    所以 run_stage 末尾会补一段 dwell，让观测窗真的达到 ``min_seconds``。
    """
    by_time = math.ceil(min_seconds / interval)
    count = max(min_requests, by_time)
    return count, max(count * interval, float(min_seconds))


def _now_cst() -> datetime:
    return datetime.now(CST)


def market_hours_warning(now: datetime) -> str | None:
    if now.weekday() >= 5:
        return None
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 15 <= minutes < 15 * 60 + 15:
        return "当前处于 A 股盘中（09:15–15:15 CST），授权明确禁止盘中压测"
    if 15 * 60 + 15 <= minutes < 16 * 60:
        return "当前处于盘后归档高峰（15:15–16:00 CST），建议避开"
    return None


def build_url(secids: list[str]) -> str:
    query = urllib.parse.urlencode(
        {"fltt": "2", "invt": "2", "fields": FIELDS, "secids": ",".join(secids)},
        safe=",",
    )
    return f"{ULIST_URL}?{query}"


def _opener(proxy: str | None) -> urllib.request.OpenerDirector:
    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def probe_once(
    opener: urllib.request.OpenerDirector,
    url: str,
    index: int,
    timeout: float,
    user_agent: str,
) -> Sample:
    started = time.monotonic()
    at = _now_cst().isoformat(timespec="seconds")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            rt = (time.monotonic() - started) * 1000
            status = response.status
    except Exception as exc:  # noqa: BLE001 - 探针要把任何异常都记成样本
        rt = (time.monotonic() - started) * 1000
        kind = _classify(exc)
        innermost = list(_exception_chain(exc))[-1]
        return Sample(
            index=index,
            at=at,
            kind=kind,
            rt_ms=rt,
            http_status=getattr(exc, "code", None),
            error=f"{type(innermost).__name__}: {innermost}"[:300],
            errno=getattr(innermost, "errno", None),
            winerror=getattr(innermost, "winerror", None),
        )

    if status != 200:
        return Sample(index=index, at=at, kind="http_error", rt_ms=rt, http_status=status)

    try:
        payload = json.loads(raw)
    except ValueError:
        return Sample(
            index=index,
            at=at,
            kind="not_json",
            rt_ms=rt,
            http_status=status,
            body=raw[:200].decode("utf-8", "replace"),
        )

    diff = ((payload or {}).get("data") or {}).get("diff") or []
    rows = len(diff) if isinstance(diff, list) else 0
    if rows == 0 and not ((payload or {}).get("data")):
        return Sample(
            index=index,
            at=at,
            kind="empty_data",
            rt_ms=rt,
            http_status=status,
            rows=rows,
            body=raw[:200].decode("utf-8", "replace"),
        )
    return Sample(index=index, at=at, kind="ok", rt_ms=rt, http_status=status, rows=rows)


def _abort_reason(stage: Stage, consecutive_fail: int, short_streak: int, baseline_p95: float | None) -> str | None:
    last = stage.samples[-1]
    if last.kind == "blocked":
        return f"上游显式风控（HTTP {last.http_status}）"
    if last.kind == "server_error":
        return f"上游 5xx（HTTP {last.http_status}）"
    if last.kind == "reset":
        return "连接被重置"
    if last.kind == "not_json":
        return "返回非 JSON（疑似风控页）"
    if last.kind == "empty_data":
        return "data 为空"
    if consecutive_fail >= 2:
        return "连续 2 次失败"
    if short_streak >= 2:
        return f"连续 2 次行数 < {stage.expected_rows}（静默截断）"
    if baseline_p95 and len(stage.samples) >= 10 and len(stage.samples) % 10 == 0:
        window = [s.rt_ms for s in stage.samples[-10:] if s.kind == "ok"]
        if len(window) == 10 and _percentile(window, 95) > 3 * baseline_p95:
            stage.slow_windows += 1
            if stage.slow_windows >= 3:
                return "p95 连续 3 个窗口 >3× 基线"
        else:
            stage.slow_windows = 0
    return None


def run_stage(
    *,
    interval: float,
    planned: int,
    seconds: float,
    opener: urllib.request.OpenerDirector,
    url: str,
    timeout: float,
    user_agent: str,
    baseline_p95: float | None,
    expected_rows: int,
    mode: str,
    log,
) -> Stage:
    stage = Stage(interval=interval, planned=planned, seconds=seconds, expected_rows=expected_rows)
    started = time.monotonic()
    consecutive_fail = 0
    short_streak = 0
    for index in range(planned):
        target = started + index * interval
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        sample = probe_once(opener, url, index, timeout, user_agent)
        stage.samples.append(sample)
        if sample.kind == "ok":
            consecutive_fail = 0
            short_streak = short_streak + 1 if (sample.rows or 0) < expected_rows else 0
        else:
            consecutive_fail += 1
            short_streak = 0
        log(
            f"  [{index + 1}/{planned}] {sample.kind:<12} {sample.rt_ms:7.1f}ms "
            f"rows={sample.rows if sample.rows is not None else '-'}"
            + (f" {sample.error}" if sample.error else "")
        )
        # census：已知出口被拒时按授权继续采满计划数，失败逐条记因，不做速率判定。
        if mode == "census":
            continue
        reason = _abort_reason(stage, consecutive_fail, short_streak, baseline_p95)
        if reason:
            stage.aborted = reason
            stage.abort_at = index + 1
            log(f"  中止：{reason}")
            break
    # 首末样本只隔 (n-1)×interval，补 dwell 让观测窗达到授权要求的时长。
    remaining = seconds - (time.monotonic() - started)
    if remaining > 0:
        log(f"  收尾观测 {remaining:.0f}s（凑满 {seconds / 60:.1f} 分钟窗口）")
        time.sleep(remaining)
    return stage


def verdict(summaries: list[dict[str, Any]], mode: str = "rate") -> dict[str, Any]:
    if mode == "census":
        return {
            "safe_interval_s": None,
            "recommended_interval_s": None,
            "supports_second_level": False,
            "note": "census 模式只记录逐条失败明细，不做安全档判定",
        }
    # 慢 → 快逐档看：第一档失败即停，安全的档位取「最快且仍通过」的那个。
    ordered = sorted(summaries, key=lambda s: -s["interval_s"])
    baseline_p95 = next((s["p95_ms"] for s in ordered if s["p95_ms"]), None)
    safe: dict[str, Any] | None = None
    for summary in ordered:
        if summary["aborted"]:
            break
        if summary["fail"] or summary["reset"] or summary["blocked"]:
            break
        if summary["row_short"]:
            break
        if summary["attempts"] < summary["planned_requests"]:
            break
        if baseline_p95 and summary["p95_ms"] and summary["p95_ms"] > 2 * baseline_p95:
            break
        safe = summary
    if safe is None:
        return {
            "safe_interval_s": None,
            "recommended_interval_s": None,
            "supports_second_level": False,
            "note": "没有任何一档满足「100% 成功 + 零 reset + 行数完整 + p95 ≤2× 基线」",
        }
    recommended = max(safe["interval_s"] * SAFETY_FACTOR, FLOOR_INTERVAL)
    return {
        "safe_interval_s": safe["interval_s"],
        "recommended_interval_s": recommended,
        "supports_second_level": recommended <= FLOOR_INTERVAL,
        "note": (
            f"安全档 {safe['interval_s']:.0f}s → 上线 {recommended:.0f}s，秒级可行，需另出「调度与面板推送」设计"
            if recommended <= FLOOR_INTERVAL
            else f"安全档 {safe['interval_s']:.0f}s → 上线 {recommended:.0f}s，维持分钟级"
        ),
        "baseline_p95_ms": baseline_p95,
    }


def render_markdown(stages: list[Stage], result: dict[str, Any], meta: dict[str, Any]) -> str:
    lines = ["# QUOT-407 ulist.np 限速压测结果", ""]
    lines.append(f"- 开始：{meta['started_at']}（CST）  结束：{meta['finished_at']}（CST）")
    lines.append(f"- 模式：{meta.get('mode', 'rate')}  出口：{meta['egress']}  目标：{ULIST_URL}  每请求 {meta['codes']} 只")
    lines.append(f"- 主机解析：{meta['resolved_ip']}  基线 p95：{meta.get('baseline_p95_ms') or '-'} ms")
    lines.append("")
    lines.append("| 间隔 | 计划 | 实际 | 成功 | 失败 | reset | 429/403 | 5xx | 行数不足 | p50 | p95 | 中止 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for stage in stages:
        s = stage.summary()
        lines.append(
            f"| {s['interval_s']:.0f}s | {s['planned_requests']} | {s['attempts']} | {s['ok']} | {s['fail']} "
            f"| {s['reset']} | {s['blocked']} | {s['server_error']} | {s['row_short']} "
            f"| {s['p50_ms'] if s['p50_ms'] is not None else '-'} | {s['p95_ms'] if s['p95_ms'] is not None else '-'} "
            f"| {s['aborted'] or '-'} |"
        )
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    if result.get("safe_interval_s") is None:
        lines.append(f"- 安全档：无。{result['note']}")
    else:
        lines.append(f"- 安全档：{result['safe_interval_s']:.0f}s（间隔）")
        lines.append(f"- 建议上线间隔：{result['recommended_interval_s']:.0f}s（安全系数 ×{SAFETY_FACTOR:.0f}，下限 10s）")
        lines.append(f"- 是否支持秒级：{'是' if result['supports_second_level'] else '否'}。{result['note']}")
    return "\n".join(lines) + "\n"


def _read_codes(args: argparse.Namespace) -> list[str]:
    if args.codes:
        raw = args.codes
    elif args.codes_file:
        raw = Path(args.codes_file).read_text(encoding="utf-8")
    else:
        raise SystemExit("需要 --codes 或 --codes-file（150 只打榜池快照 secid）")
    items = [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
    return list(dict.fromkeys(items))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QUOT-407 单路径限速探针（只读，不改闸门常量）")
    parser.add_argument("--codes", help="逗号分隔 secid，如 1.600000,0.000001")
    parser.add_argument("--codes-file", help="secid 列表文件（逗号或换行分隔）")
    parser.add_argument("--stages", default=",".join(str(int(s)) for s in STAGES), help="阶梯间隔秒，默认 15,30,60")
    parser.add_argument("--min-seconds", type=int, default=DEFAULT_MIN_SECONDS)
    parser.add_argument("--min-requests", type=int, default=DEFAULT_MIN_REQUESTS, help="次数下限覆盖；默认按 600÷间隔 换算")
    parser.add_argument("--mode", choices=MODES, default="rate", help="rate=遇信号即停；census=已知出口被拒时采满计划数并逐条记因")
    parser.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN, help="档间冷却秒，默认 600")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--proxy", help="独立测试出口，如 http://10.0.0.9:3128")
    parser.add_argument("--user-agent", default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36")
    parser.add_argument("--json-out", help="逐条样本 JSON 输出路径")
    parser.add_argument("--md-out", help="Markdown 报告输出路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不发请求")
    parser.add_argument("--force", action="store_true", help="跳过盘中保护（授权不允许盘中，慎用）")
    args = parser.parse_args(argv)

    secids = _read_codes(args)
    intervals = [float(part) for part in args.stages.split(",") if part.strip()]
    plans = [(interval, *plan_stage(interval, args.min_seconds, args.min_requests)) for interval in intervals]
    total_requests = sum(item[1] for item in plans)
    total_seconds = sum(item[2] for item in plans) + args.cooldown * max(0, len(plans) - 1)
    try:
        resolved = socket.gethostbyname(urllib.parse.urlparse(ULIST_URL).hostname or "")
    except OSError as exc:
        # DNS 失败本身也是出口证据，不能让它把整轮压测打挂。
        resolved = f"unresolved: {exc}"

    print(f"目标：{ULIST_URL}")
    print(f"每请求 {len(secids)} 只；预计 {total_requests} 次请求，含冷却约 {total_seconds / 60:.1f} 分钟")
    print(f"主机解析：{resolved}；出口：{args.proxy or '直连'}")
    for interval, count, seconds in plans:
        print(f"  {interval:>5.0f}s 档：{count} 次 / {seconds / 60:.1f} 分钟")
    if len(secids) != EXPECTED_ROWS:
        print(f"注意：secid 数量 {len(secids)}，生产批量是 {EXPECTED_ROWS}，行数断言按当前数量折算")

    warning = market_hours_warning(_now_cst())
    if warning:
        print(f"警告：{warning}")
        if not args.force:
            print("拒绝执行。确认授权后加 --force。")
            return 2

    if args.dry_run:
        print("dry-run：未发请求。")
        return 0

    opener = _opener(args.proxy)
    url = build_url(secids)
    expected = min(EXPECTED_ROWS, len(secids))
    stages: list[Stage] = []
    started_at = _now_cst().isoformat(timespec="seconds")
    baseline_p95: float | None = None

    for position, (interval, count, seconds) in enumerate(plans):
        if position:
            print(f"冷却 {args.cooldown:.0f}s …")
            time.sleep(args.cooldown)
        print(f"=== {interval:.0f}s 档：{count} 次 ===")
        stage = run_stage(
            interval=interval,
            planned=count,
            seconds=seconds,
            opener=opener,
            url=url,
            timeout=args.timeout,
            user_agent=args.user_agent,
            baseline_p95=baseline_p95,
            expected_rows=expected,
            mode=args.mode,
            log=print,
        )
        stages.append(stage)
        if position == 0:
            lat = stage.latencies()
            baseline_p95 = _percentile(lat, 95) if lat else None
        if stage.aborted:
            print(f"{interval:.0f}s 档中止，回落到上一档结论。")
            break

    summaries = [stage.summary() for stage in stages]
    result = verdict(summaries, args.mode)
    meta = {
        "started_at": started_at,
        "finished_at": _now_cst().isoformat(timespec="seconds"),
        "mode": args.mode,
        "egress": args.proxy or "直连",
        "resolved_ip": resolved,
        "codes": len(secids),
        "expected_rows": expected,
        "baseline_p95_ms": baseline_p95,
        "min_seconds": args.min_seconds,
        "min_requests": args.min_requests,
        "cooldown_s": args.cooldown,
    }

    if args.json_out:
        payload = {
            "meta": meta,
            "stages": [
                {**summary, "samples": [sample.__dict__ for sample in stage.samples]}
                for summary, stage in zip(summaries, stages)
            ],
            "verdict": result,
        }
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    markdown = render_markdown(stages, result, meta)
    if args.md_out:
        Path(args.md_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md_out).write_text(markdown, encoding="utf-8")
    print()
    print(markdown)
    return 0 if result["safe_interval_s"] is not None else 1


if __name__ == "__main__":
    sys.exit(main())
