# Peer 部署 Runbook：venv 安装形态纪律

适用：TickHub-CN / TickHub-US / quotafuse-wire / StockSenate 四个 peer 的部署机 venv（本篇为 TickHub-CN 份）。
最后核对：**2026-11-05**（QUOT-401 B 部分）。

## 硬规则（本机部署一律遵守）

1. 四个 peer 的 venv 里，本模块包**只允许两种形态**：editable 安装（`pip install -e .`）或安装 wheel。**禁止**把源码包目录拷进 site-packages（站点包副本）。
2. **换分支 / 换 tag / pull 之后必须重装**：`git checkout` 只改工作区，site-packages 里的旧副本不会跟着变，`importlib.metadata` 读到的版本、`__version__` 与实际源码会不一致——TickHub-CN 这次「healthz 版本不对」的坑即由此而来。
3. 判定优先用文件证据，不靠 `pip list`（它对副本与 editable 都显示"已安装"）。

## 本模块速查

| 项 | 值 |
|---|---|
| 仓库（部署机） | `D:\workplace\TickHub-CN` |
| venv | `D:\workplace\TickHub-CN\.venv`（Python 3.12） |
| 包名 / 当前版本 | `tickhub_cn` / `1.1.0` |
| 计划任务 | `TickHub CN 8000`（API）、`TickHub CN Admin 8787` |

## 如何判定安装形态（30 秒）

```powershell
ls D:\workplace\TickHub-CN\.venv\Lib\site-packages\ | findstr /i "tickhub"
```

- 有 `__editable__.tickhub_cn-*.pth`、无实体包目录（没有 `tickhub_cn\` 文件夹）→ **editable，合规**。
- 出现实体包目录 `tickhub_cn\` → **站点包副本，违规**，按下面重装。

本机核对结果（2026-11-05）：

```
__editable__.tickhub_cn-1.1.0.pth
tickhub_cn-1.1.0.dist-info
```

→ editable，合规。

## 违规后如何重装（editable）

```powershell
cd D:\workplace\TickHub-CN
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
```

重装前先删掉 site-packages 里的实体包副本目录与同名 `*.dist-info`（editable 的 `.pth` 与副本并存时，import 解析顺序不可预期）。

改用 wheel 时：`.\.venv\Scripts\python.exe -m pip install <path-to>\tickhub_cn-1.1.0-py3-none-any.whl --force-reinstall --no-deps`。二选一，不要混用。

## 换分支后的固定动作

```powershell
cd D:\workplace\TickHub-CN
git checkout <branch>          # 或 git fetch --tags && git checkout v1.1.0
.\.venv\Scripts\python.exe -m pip install -e . --no-deps   # 必重装
# 重启只按自己起的 PID 杀，不要按进程名
```

## 相关文档

- `docs/CI.md`：CI 门禁与本地等价命令。
- 版本唯一真源：`pyproject.toml` 的 `[project] version`；`__version__` 从 `importlib.metadata` 读；`/healthz` 回 `{"status","service","version"}`。
