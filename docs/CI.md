# CI 门禁：实际命令、本地等价命令与 required checks

本文件描述 `.github/workflows/ci.yml` 的真实行为，以及在本机复现同一门禁的命令。
最后核对：**2026-10-31**（`main@bcd2ce0`，CI 绿）。

## 一、CI 实际跑什么

触发：`push`（所有分支）+ `pull_request`。同一 ref 的新运行会取消旧运行（`concurrency.cancel-in-progress`）。

权限：`permissions: contents: read`。只使用仓库内置的 `GITHUB_TOKEN`，**不需要配置任何 secret**。

流程级环境变量：`PYTHONIOENCODING=utf-8`、`PIP_DISABLE_PIP_VERSION_CHECK=1`。

### Job `test`（4 个矩阵组合）

矩阵：`os = [ubuntu-latest, windows-latest]` × `python = [3.11, 3.12]`，`fail-fast: false`。每个组合的 check 名形如 `test (ubuntu-latest, py3.11)`。

每个组合执行：

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev,admin,notes]"

# Import smoke test
python -c "import tickhub_cn, tickhub_cn.datasources; from tickhub_cn.mcp.server import tool_count; assert tool_count() == 149, tool_count()"

# Run tests
python -m pytest
```

`admin` extra 必须装（`admin_api` 在测试收集期被导入），`notes` extra 也必须装（cninfo 附注回归测试要解析真实 PDF）。测试套件是离线的：`conftest.py` 把 Redis 指向不可路由端口、切到临时目录、且不设 `TICKHUB_LIVE`，所以默认不联网；live 用例会自动 skip。

### Job `catalog`

`ubuntu-latest` + Python 3.12，检查生成物是否漂移：

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

python scripts/gen_tool_catalog.py --format skill --out skills/tickhub-cn/references/tools.md
git diff --exit-code -- skills/tickhub-cn/references/tools.md
```

工具注册表有改动却没重新生成 `tools.md` 时，这个 job 会红。

## 二、本地等价命令

前置：Python ≥ 3.11。CI 覆盖 Linux + Windows 两套系统；本地只能覆盖当前系统，跨平台差异由 CI 的矩阵兜底。

### Windows（PowerShell / cmd）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,admin,notes]"

.\.venv\Scripts\python.exe -X utf8 -c "import tickhub_cn, tickhub_cn.datasources; from tickhub_cn.mcp.server import tool_count; assert tool_count() == 149, tool_count()"
.\.venv\Scripts\python.exe -X utf8 -m pytest
```

### macOS / Linux

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e ".[dev,admin,notes]"

./.venv/bin/python -c "import tickhub_cn, tickhub_cn.datasources; from tickhub_cn.mcp.server import tool_count; assert tool_count() == 149, tool_count()"
./.venv/bin/python -m pytest
```

venv 解释器路径差异：Windows 是 `.venv\Scripts\python.exe`，POSIX 是 `.venv/bin/python`。Windows 控制台建议加 `-X utf8`（CI 用 `PYTHONIOENCODING=utf-8` 达到同样效果）。

### catalog 检查（与 CI 的 `catalog` job 等价）

```powershell
# Windows
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts/gen_tool_catalog.py --format skill --out skills/tickhub-cn/references/tools.md
git diff --exit-code -- skills/tickhub-cn/references/tools.md
```

```bash
# macOS / Linux
./.venv/bin/python -m pip install -e ".[dev]"
./.venv/bin/python scripts/gen_tool_catalog.py --format skill --out skills/tickhub-cn/references/tools.md
git diff --exit-code -- skills/tickhub-cn/references/tools.md
```

### 联网用例（不属于门禁）

```bash
pytest -m live        # 需要真实上游网络，不会被 PR 门禁执行
```

### 本机实测（2026-10-31，Windows 10 / Python 3.12.10）

- import smoke：`tool_count() == 149` 通过
- `pytest`：421 passed, 15 skipped（160.69s）
- catalog：生成 149 个名称，`git diff --exit-code` 无漂移

## 三、required checks 现状

**当前未设为 required，仅为参考。**

核对方式（2026-10-31，公开 API）：

- `GET /repos/QuotaFuse-ai/TickHub-CN/branches/main` → `"protected": false`
- `GET /repos/QuotaFuse-ai/TickHub-CN/rulesets` → `[]`

即 main 既没有 branch protection，也没有 repository ruleset。CI 会跑并给出红绿信号，但**不会阻止合并**；要获得强制力，需要仓库管理员在 Settings → Branches（或 Rulesets）里配置。

若管理员启用保护，应把下面 5 个 check 全部设为 required，名称需逐字一致：

- `test (ubuntu-latest, py3.11)`
- `test (ubuntu-latest, py3.12)`
- `test (windows-latest, py3.11)`
- `test (windows-latest, py3.12)`
- `tool catalogue is current`

只勾一部分会留下未受保护的平台/版本组合。配置后本文件需同步更新为实际值。
