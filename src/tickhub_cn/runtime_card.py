"""自述卡：把「我在哪个端口服务」写进安装目录，供 LadderStock 发现。

背景（2026-11-21）：LadderStock 过去按「基础端口 + 环境偏移」推算子模块地址。
一台机器上通常只有**一份共用实例**，推算出来的 18000/18790/18765 全是空门，
面板于是把 6 个在跑的模块一起报成「已停止」。改成「谁提供服务谁登记」：
模块启动时把真实端口写进 `<安装目录>/.runtime/peer.json`，客户端只读这张卡。

卡片跟着安装目录走：换盘符、换端口、换机器、换安装路径都不用改配置。
格式与 `ladderstock.core.discovery` 是同一份契约（面板读的就是它）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CARD_DIR = ".runtime"
CARD_NAME = "peer.json"


@contextmanager
def _card_lock():
    """同一模块的两个进程（MCP 面 / Web 面）会同时启动并各写一次卡。

    没有锁时「读旧卡 → 合并 → 原子替换」会丢字段：后写者读到的是对方写之前的
    空卡，于是 senate 的卡只剩 mcp、web 面被抹掉（2026-11-21 实测）。锁文件独立，
    超时就照常写（宁可少合并，也不让登记卡住服务启动）。
    """
    lock = card_root() / CARD_DIR / "peer.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock, "a+b")
    try:
        f.seek(0)
        if f.read(1) == b"":
            f.seek(0)
            f.write(b"x")
            f.flush()
        deadline = time.monotonic() + 2.0
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def card_root() -> Path:
    """卡片落点：环境变量 > 当前目录（自己就是模块安装根）> 含 pyproject.toml 的祖先。

    控制面拉起时会注入 `LDST_PEER_CARD_ROOT=<模块安装目录>`，那是唯一不会读错的
    位置；手工从模块目录启动时，cwd 就是模块根，同样写对。
    """
    override = os.environ.get("LDST_PEER_CARD_ROOT")
    if override:
        return Path(override)
    cwd = Path.cwd()
    if (cwd / "pyproject.toml").is_file() or (cwd / CARD_DIR).is_dir():
        return cwd
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return here.parent


def card_path() -> Path:
    return card_root() / CARD_DIR / CARD_NAME


def _read() -> dict[str, Any]:
    try:
        raw = json.loads(card_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def announce(
    module: str,
    *,
    mcp: int | None = None,
    web: int | None = None,
    extra: Iterable[int] = (),
    product: str | None = None,
    version: str | None = None,
) -> Path | None:
    """登记（或合并）本模块的真实端口；只传本进程负责的那些面。

    同一模块可能由两个进程分别提供 MCP 面和 Web 面，所以这里读旧卡再合并，
    不会把对方刚写进去的面覆盖掉。写临时文件再 `os.replace`，面板永远读不到半个 json。
    失败一律静默：登记只是「让面板找得到」，不能反过来影响服务启动。
    """
    path = card_path()
    try:
        with _card_lock():
            payload = _read()
            ports = payload.get("ports") if isinstance(payload.get("ports"), dict) else {}
            if mcp:
                ports["mcp"] = int(mcp)
            if web:
                ports["web"] = int(web)
            merged_extra = {int(item) for item in ports.get("extra") or () if item}
            merged_extra.update(int(item) for item in extra if item)
            if merged_extra:
                ports["extra"] = sorted(merged_extra)
            payload.update(
                {
                    "module": module,
                    "ports": ports,
                    "pid": os.getpid(),
                    "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            )
            payload.setdefault("started_at", payload["updated_at"])
            if product:
                payload["product"] = product
            if version:
                payload["version"] = version
            payload.setdefault("env", "test" if os.environ.get("LDST_PORT_OFFSET") else "prod")

            path.parent.mkdir(parents=True, exist_ok=True)
            # 临时文件按 pid 分开：两个进程如果在同一 tmp 上写/替换，后一个会拿到
            # Permission denied（2026-11-21 senate 实测）。os.replace 仍是原子换入。
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        return path
    except Exception:  # noqa: BLE001 - 登记失败不许影响服务本身
        print(f"peer card 写入失败（忽略）：{sys.exc_info()[1]}", file=sys.stderr)
        return None


def withdraw() -> None:
    """进程退出时撤卡：删掉本卡，避免把「已停的服务」继续当成在线。"""
    try:
        card_path().unlink(missing_ok=True)
    except OSError:
        pass


__all__ = ["announce", "card_path", "card_root", "withdraw"]
