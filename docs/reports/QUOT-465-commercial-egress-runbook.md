# QUOT-465 商业出口复测 ulist 安全速率：就绪状态与执行手册

- 日期：2026-11-06
- 状态：**已于 2026-11-06 晚间完成一次复测**（51代理付费出口，15s 档 40 次 / 711s / 0 失败），
  结论见 `docs/decisions/QUOT-407-refresh-frequency.md` §5。本手册 §1–§4 的预检与前置要求仍然有效，
  用于下一次固定单 IP 出口的复测；§1 记录的是当日上午的未开跑状态。
- 上游口径：`docs/decisions/QUOT-407-refresh-frequency.md`（§2 样本量、§3 出口封锁处置、§5 复测结论）
- 上一轮实测：`docs/reports/QUOT-407-ulist-rate-probe.md`（15s 档 0/40，出口 RST）

## 1. 触发条件核查（2026-11-06 12:29 CST）

| 检查项 | 结果 |
| --- | --- |
| provider 可插拔实现 | ✅ 已合入 `main`（`edbf7a1`，`PROXY_PROVIDER=direct/pool/commercial`） |
| 商业出口配置 `PROXY_COMMERCIAL_URL` | ❌ 仓库与本文档所在环境均无值；`.env.example` 此前也没有这几项（本次已补） |
| 本机出口是否恢复 | ❌ 单请求 `push2` ulist：`RemoteDisconnected`，RT 242ms（与 §3 的 78–250ms 秒断特征一致） |
| 本机出口 IP | 120.244.39.132（北京移动），仍落在此前封锁范围 |

结论：在拿到商业出口前，任何复跑都只会重复「0 有效样本 + 100% 连接重置」。
不要在本机重试，也不要靠「换个时间碰运气」。

## 2. 开跑前置（需人工提供）

1. 商业代理账号/合同，且供应商条款允许对目标站做 10 分钟窗口、约 40–70 次请求的压测。
2. 网关信息：协议（须 HTTP/HTTPS CONNECT）、地址端口、认证方式（账号密码或来源 IP 白名单）。
3. **固定出口**：确认网关在单次 600s 窗口内保持同一出口 IP。轮换网关只能测「网关整体」，
   不能产出单 IP 安全速率（探针按出口 IP 出结论，轮换会让结论失去意义）。
4. 部署侧配置后自检：

```bash
PROXY_PROVIDER=commercial
PROXY_COMMERCIAL_URL=http://user:pass@gate.example.com:8000
th-cn doctor          # proxy_exit 必须显示 commercial url=set
```

5. 出口预检（探针只记录目标域名的**本地**解析 IP，不记录真实出口，必须人工补这一条）：

```bash
curl -x "$PROXY_COMMERCIAL_URL" -m 10 https://myip.ipip.net   # 记下出口 IP
curl -x "$PROXY_COMMERCIAL_URL" -m 10 -o /dev/null -w '%{http_code} %{time_total}\n' \
  'https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=1.600000&fields=f12,f14,f2,f3,f62,f184'
```

单请求不是 200/JSON、或出口 IP 仍是 120.244.39.132 一类已封地址，就停，不要进入阶梯。

## 3. 采集

- 真实打榜池 150 只 secid 快照（上一轮用的是 `1.600000` 起的占位代码，仅因 0 响应而结论不受影响）。
- 时段：A 股盘中 09:15–15:15 授权禁止压测；15:15–16:00 盘后归档高峰也避开。
- 先只跑 15s 档（40 次 / ≥600s），样本达标再考虑 30s、60s：

```bash
python scripts/ulist_rate_probe.py \
  --codes-file logs/quot407-secids.txt \
  --proxy "$PROXY_COMMERCIAL_URL" \
  --mode rate \
  --stages 15 \
  --min-seconds 600 \
  --json-out logs/quot465-15s.json \
  --md-out logs/quot465-15s.md
```

- `--mode rate`：遇 429/403、5xx、连接重置、连续 2 次失败等信号立即中止并回落，是求安全速率的唯一模式。
- 档间冷却默认 600s；跑 15/30/60 三档总时长约 50 分钟。
- 有效样本只计真实到达上游的请求；缓存命中、闸门/熔断拒绝不计；被上游拒绝计**失败**并标原因。

## 4. 判读与回写

1. `verdict` 给出安全间隔、建议上线间隔（安全系数 ×2、下限 10s）、是否支持秒级。
2. 若某档有效样本不足：结论写「出口所致，不是上游速率上限」，不给该档数值，不调闸门常量。
3. 回写 `QUOT-407-refresh-frequency.md` §4：新增一条带日期的「复测结果」，
   写明出口 IP、时段、样本量、失败分类、安全速率；**不覆盖 2026-11-05 的原始结论**。
4. 秒级（5s/1s）只有在 ≥60 有效样本、且无 429/403/重置时才评估放开；
   放开即改 LadderStock `VITE_LDST_CHART_REFRESH_SECOND_LEVEL`，属单独审批项，需老板拍板。

## 5. 已知就绪缺口

| 缺口 | 影响 | 处置 |
| --- | --- | --- |
| 探针只支持 HTTP/HTTPS 代理（`urllib`，无 SOCKS） | 商业池若只给 `socks5://`，探针跑不了 | 供应商选 HTTP CONNECT 网关；或另开单加 PySocks |
| 无出口 IP 记录/sticky 校验 | 报告无法自证「请求经商业出口、窗口内同 IP」 | 用 §2.5 的 curl 预检人工取证并写进报告 |
| `.env.example` 未列 provider 变量 | 运维无法按模板配置商业出口 | 本单已补（README §代理配置 同步补充） |
