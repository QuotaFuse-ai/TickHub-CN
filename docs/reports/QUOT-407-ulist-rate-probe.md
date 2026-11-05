# QUOT-407 ulist.np 刷新频率口径与 15s 档实测报告

- 日期：2026-11-05（口径更新与 15s 档实测：23:12–23:22 CST）
- 状态：**口径已落地、15s 档已实测**。当前出口 0/40 有效样本，原因是 `push2` 族对云机房出口的主机级封锁（已知限制，不阻塞本轮）。
- 相关：熔断/授权口径见 `docs/decisions/QUOT-407-endpoint-breaker-semantics.md`；频率口径见 `docs/decisions/QUOT-407-refresh-frequency.md`；探针见 `scripts/ulist_rate_probe.py`

## 1. 口径结论（2026-11-05 老板拍板）

### 1.1 刷新频率：放弃秒级，当前用 15s 档

- 当前阶段图表刷新用 **15s 档**，不做 1s / 5s 秒级刷新。
- 秒级档位**暂缓**，等商业化 IP 代理池就位后再启用：商业池以 `provider: pool/commercial/direct` 可插拔接入，`commercial_url` 填链接即可，相关实现在代码里保留、默认不启用。
- 刷新频率做成配置项，并暴露为图表上的下拉档位：**15s / 30s / 60s**，当前默认 **15s**；秒级档位（5s / 1s）保留在配置中，默认隐藏。

### 1.2 样本量验收口径（替换原「每档 ≥10 分钟且 ≥60 次」）

原口径两项条件在慢档上互相矛盾（60 次 × 50s = 50 分钟），现按「600 秒窗口换算」统一，并一律保留 ≥10 分钟时长下限：

| 档位 | 有效样本下限 | 运行时长下限 | 换算 |
| --- | --- | --- | --- |
| 15s | 40 | ≥10 分钟 | 600 ÷ 15 |
| 30s | 20 | ≥10 分钟 | 600 ÷ 30 |
| 60s | 10 | ≥10 分钟 | 600 ÷ 60 |
| 秒级（启用后） | 60 | ≥10 分钟 | 商业池上线后改回 ≥60 次 |

公式：`次数 = 600 秒 ÷ 档位秒数`（向上取整），时长一律 ≥600s。秒级档位启用时统一改回「≥60 次」。

### 1.3 push2 出口封锁：已知限制，不阻塞本轮

- 用 15s 档在当前出口继续采集，能拿到的真实样本照常记录；
- 被 `push2` 拒绝的请求**计入失败并标注原因**，不因此中止本轮、不改闸门常量。

## 2. 代码落地：频率配置项 + 图表下拉

**已落地，PR 已开、CI 绿**：LadderStock PR #111（`feat(market): 图表自动刷新频率可配置，默认 15s（QUOT-407）`，commit `6705fda`，CI 两项检查全绿）。

| 文件（LadderStock） | 内容 |
| --- | --- |
| `frontend/src/lib/chartRefresh.ts` | 档位常量（15/30/60，秒级 5/1 单独一组）、localStorage 持久化（`ldst.chart.refreshSec`）、默认值 15s、`useChartRefresh()` / `useAutoRefresh()` |
| `frontend/src/screens/MarketScreen.tsx` | 市场页工具栏新增「自动刷新」Segmented（15s/30s/60s），按所选档位定时 `reloadAll` |
| `frontend/tests/chart-refresh.test.tsx` | 8 条用例：默认档位、秒级默认不可见、非法存档回落 15s、持久化、未开放档位拒绝、15s 触发 14s 不触发、页面隐藏时不触发、关闭时不触发 |

配置项（Vite env）：

| Key | 作用 | 默认 |
| --- | --- | --- |
| `VITE_LDST_CHART_REFRESH_SEC` | 初始档位 | 15 |
| `VITE_LDST_CHART_REFRESH_SECOND_LEVEL` | 是否放开秒级档位（`1` / `true`） | 关闭 |

浏览器实测（dev server `127.0.0.1:5199`）：市场页渲染出「自动刷新 15s / 30s / 60s」，默认选中 15s；点击 30s 后 `localStorage['ldst.chart.refreshSec']` 写入 `"30"`，重开页面保持 30s。定时触发本身由单元测试覆盖（观察窗被浏览器重连打断，未取到实时 tick 截图）。

**必须知道的一处落差**：15s 是**界面刷新**频率，不等于 15s 的**上游数据新鲜度**。TickHub `stock_fund_flow_batch` 的缓存 TTL 为 60s、端点族闸门为同族 25s 间隔，因此 15s 刷新多数会命中缓存；要拿到真正 15s 粒度的上游数据，需要下调 TTL 与闸门常量（属另一个审批项，本单不动）。

## 3. 15s 档实测（当前出口，census 模式）

- 时段：2026-11-05 23:12:27 → 23:22:27 CST（600s，满足 ≥10 分钟）
- 出口：直连；目标解析 `push2.eastmoney.com` → 117.184.38.132
- 单请求：150 只 secid、`fields=f12,f14,f2,f3,f62,f184`
- 节奏：按 `start + k × 15s` 对齐

### 3.1 汇总

| 计划 | 实际发起 | 有效样本 | 失败 | reset | 超时 | 429/403 | 5xx | 行数不足 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 40 | 40 | **0** | 40 | 39 | 1 | 0 | 0 | 0 |

- **有效样本 0 次**，未达 15s 档「≥40 有效样本」；**时长 600s 已满足**。
- 失败率 100%，其中 97.5% 为连接重置、2.5% 为 10s 超时；**没有任何 HTTP 响应**（连 429/403 都没有），即连接建立后、响应前被对端关闭。

### 3.2 失败明细

| # | 时刻 | 结果 | RT(ms) | 异常 |
| --- | --- | --- | --- | --- |
| 0 | 23:12:27 | reset | 125 | RemoteDisconnected |
| 1 | 23:12:42 | reset | 94 | RemoteDisconnected |
| 2 | 23:12:57 | reset | 94 | RemoteDisconnected |
| 3 | 23:13:12 | reset | 3141 | RemoteDisconnected |
| 4 | 23:13:27 | reset | 172 | RemoteDisconnected |
| 5 | 23:13:42 | reset | 141 | RemoteDisconnected |
| 6 | 23:13:57 | reset | 94 | RemoteDisconnected |
| 7 | 23:14:12 | reset | 78 | RemoteDisconnected |
| 8 | 23:14:27 | reset | 3094 | RemoteDisconnected |
| 9 | 23:14:42 | reset | 78 | RemoteDisconnected |
| 10 | 23:14:57 | reset | 1109 | RemoteDisconnected |
| 11 | 23:15:12 | reset | 109 | RemoteDisconnected |
| 12 | 23:15:27 | reset | 94 | RemoteDisconnected |
| 13 | 23:15:42 | reset | 109 | RemoteDisconnected |
| 14 | 23:15:57 | reset | 1203 | RemoteDisconnected |
| 15 | 23:16:12 | reset | 156 | RemoteDisconnected |
| 16 | 23:16:27 | reset | 187 | RemoteDisconnected |
| 17 | 23:16:42 | reset | 156 | RemoteDisconnected |
| 18 | 23:16:57 | reset | 125 | RemoteDisconnected |
| 19 | 23:17:12 | reset | 1172 | RemoteDisconnected |
| 20 | 23:17:27 | reset | 203 | RemoteDisconnected |
| 21 | 23:17:42 | reset | 3344 | RemoteDisconnected |
| 22 | 23:17:57 | transport | 10000 | TimeoutError: timed out |
| 23 | 23:18:12 | reset | 203 | RemoteDisconnected |
| 24 | 23:18:27 | reset | 218 | RemoteDisconnected |
| 25 | 23:18:42 | reset | 219 | RemoteDisconnected |
| 26 | 23:18:57 | reset | 250 | RemoteDisconnected |
| 27 | 23:19:12 | reset | 3375 | RemoteDisconnected |
| 28 | 23:19:27 | reset | 187 | RemoteDisconnected |
| 29 | 23:19:42 | reset | 172 | RemoteDisconnected |
| 30 | 23:19:57 | reset | 172 | RemoteDisconnected |
| 31 | 23:20:12 | reset | 172 | RemoteDisconnected |
| 32 | 23:20:27 | reset | 3156 | RemoteDisconnected |
| 33 | 23:20:42 | reset | 94 | RemoteDisconnected |
| 34 | 23:20:57 | reset | 78 | RemoteDisconnected |
| 35 | 23:21:12 | reset | 109 | RemoteDisconnected |
| 36 | 23:21:27 | reset | 109 | RemoteDisconnected |
| 37 | 23:21:42 | reset | 7234 | RemoteDisconnected |
| 38 | 23:21:57 | reset | 94 | RemoteDisconnected |
| 39 | 23:22:12 | reset | 109 | RemoteDisconnected |

RT 分布：≤250ms 30 次（78–250ms，中位 172ms）、1.1–3.4s 8 次、7.2s 1 次、10s 超时 1 次；均值 1026ms。

### 3.3 判读

- 30/40 在 78–250ms 内断开，说明 TCP 已建立、应用层立即 RST：**不是超时、不是排队，也不是速率触发的限速**；若是限速通常会先给 429/403 或慢响应。
- 9 次拖到 1.1–7.2s 才断、1 次耗尽 10s 超时，符合边缘节点对封禁来源「有时秒断、有时挂起」的间歇策略。
- 全程 0 个 429/403/5xx，**不能据此推断上游速率上限**；这仍是出口不可用，不是东财对 `ulist.np` 的速率结论。
- 与 11-03 打榜时那 80 只连接重置不同：那批落在两个坏代理节点、换节点即成功；本轮直连出口是同一台宿主机、40 次持续被断，属出口级封锁。

## 4. 结论

1. **15s 档产品口径成立**：默认 15s，配置项与图表下拉已落地（LadderStock PR #111），秒级档位保留但不启用。
2. **15s 档实测在当前出口拿不到有效样本**（0/40），「≥40 有效样本」未达成；运行时长 600s 已满足。数据缺口原因明确为 `push2` 族对云机房出口的主机级封锁，按老板口径记为**已知限制**，不计入上游速率结论。
3. **维持现有闸门常量**（`ENDPOINT_MIN_INTERVAL=25s`、60s 窗口 ≤3 次、熔断 600s）；没有任何证据支持放宽。界面 15s 刷新在上游侧实际受 25s 闸门与 60s 缓存 TTL 约束。
4. 秒级（1s/5s）继续暂缓，待商业化 IP 代理池（`pool/commercial/direct`）上线后按「≥60 有效样本 + ≥10 分钟」重新验收。

## 5. 复现与后续采集

```bash
python scripts/ulist_rate_probe.py \
  --stages 15,30,60 \
  --mode census \
  --codes-file logs/quot407-secids.txt \
  --min-seconds 600 \
  --json-out logs/quot407-census-15s.json \
  --md-out logs/quot407-census-15s.md
```

- 本轮 150 只 secid 为占位代码（`1.600000` 起）；连接在收到任何响应前即被断开，证券代码不影响失败结论。拿到可用出口后应改用真实打榜池快照。
- `--mode census` 只记录逐条失败明细、不做安全档判定；出口可用后改用 `--mode rate` 才能给出安全/上线间隔。
- 商业池就位后，加 `--proxy http://<commercial-egress>:port` 复跑，并把秒级档位加入 `--stages 15,30,60,5,1`。
