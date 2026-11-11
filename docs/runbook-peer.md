# 对端部署 Runbook（peer venv）

适用：把本模块部署到 LadderStock 对端机器、且已经存在 `.venv` 的场景。

## 安装形态：只允许 editable 或 wheel，禁止拷贝源码

对端 venv 里的包必须来自 `pip install -e .`（现场开发/联调）或 `pip install <name>-<version>-py3-none-any.whl`（发布版），
**不要把 `src/` 拷进 `site-packages`**。拷贝安装会让 `site-packages` 里出现物理包目录，代码改了不生效、版本号也对不上，排查极耗时。

自检（对端 venv 里执行，应当**没有任何输出**）：

```bash
ls .venv/Lib/site-packages | grep -E "^(tickhub|stock_senate|qf_wire|quota_fuse)" || echo clean
```

有输出说明是拷贝安装，删掉物理目录后按下面的方式重装。

## 切换分支后必须重新安装

editable 安装的入口点、依赖解析都记录在安装时那一刻。**切换分支后必须重新执行 `pip install -e .`**，
否则可能出现「代码是新的、依赖是旧的」或「入口脚本仍指向旧模块」：

```bash
git fetch origin && git checkout main && git pull --ff-only
.venv/Scripts/python -m pip install -e .        # Windows
# .venv/bin/python -m pip install -e .          # Linux/macOS
```

从拷贝安装迁到 editable 时，先删物理目录再装：

```bash
rm -rf .venv/Lib/site-packages/<package_name>
.venv/Scripts/python -m pip install -e .
```

## 版本与健康检查

- 版本单一真源是 `pyproject.toml` 的 `[project] version`，代码里 `__version__` 从 `importlib.metadata` 读取，不要在代码里写版本字面量。
- 重启后先看 `/healthz` 确认「跑的是哪一版」：

```bash
curl -s http://127.0.0.1:<port>/healthz
# {"status":"ok","service":"<service>","version":"<x.y.z>"}
```

返回的 `version` 与 `pyproject.toml` 不一致，多半是 venv 里装的还是旧包（或拷贝安装），按上面的方式重装。

## 发布物

```bash
PYTHON=.venv/Scripts/python.exe bash scripts/build-release.sh
# 产出 dist-build/<name>-<version>-py3-none-any.whl 与 dist-build/SHA256SUMS
```

tag 必须是 `v<pyproject version>`，Release 附件只放 `*.whl` 与 `SHA256SUMS`。
