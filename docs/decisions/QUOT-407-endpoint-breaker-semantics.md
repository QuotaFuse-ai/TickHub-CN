# 端点族熔断与限流退避语义定论（QUOT-407）

- 日期：2026-11-05
- 基线：TickHub-CN `main@8ecb8dc`（v1.1.0）；QUOT-370 的只读计数在 PR #3，未合并，本文件只把它当观测口径引用
- 范围：`push2*.eastmoney.com` 的 push2 端点族（`core/http.py`）与 LadderStock 侧退避消费端（只读，不改）
- 不在本单：改闸门常量、改 QUOT-370 的计数/重试实现、动 LadderStock 执行路径

## 0. 先对齐现状（代码事实）

| 机制 | 值 / 行为 | 位置 |
| --- | --- | --- |
| 端点族 | `host + path`，`.np` 后缀归一，`kamt/get`、`slist/get` 豁免 | `core/http.py:126-135` |
| 端点闸门 | 同族最小间隔 25s、60 秒窗口 ≤3 次、单次最多等 2s 就降级 | `core/http.py:118-123, 142-148, 438-450` |
| 熔断 | 连接重置即 trip，冷却 600s，按族计、不分节点 | `core/http.py:151-193, 488-499` |
| 重试 | `HTTP_RETRIES=2`，每次 `fetch` 最多 3 次真实往返；`PROXY_FAULTS` 只含 ProxyError/ConnectTimeout/SSLError 时换节点 | `core/config.py:58`, `core/http.py:353-357, 452-502` |
| 客户端退避 | 一次取数共享预算：最多 2 次、单次 ≤15s；冷却 = `min(max(retry_after_s, cooldown_sec), 900)` | LadderStock `provider_registry.py:750-770, 1029-1069, 1116-1126` |

两个容易忽略的实现事实：

1. `endpoint_limiter(...).acquire()` 在**重试循环之外**，每次 `fetch` 只占一个槽位；循环内的换节点重试不再过闸门（`core/http.py:438` vs `:455`）。
2. 连接重置时既不 `pool.mark(node, False)` 也不把节点加入 `tried`，所以坏节点不会被排除，只是整族被熔断（`core/http.py:485-499`）。

## 1. 连接重置时「绕过换节点、直接 trip 端点族」是否保留

**定论：不保留，改为有界换节点——同一调用内先换一个健康节点重试一次，仍 reset 才 trip 600s。**

依据：

- 2026-11-03 打榜资金实测量到的那次「80 只连接重置」，落在两个不同的坏代理节点上，换节点重试即成功（QUOT-367 父单记录）。单次 reset 不能证明是上游按端点封路。
- 代价不对称：一次 failover 只多 1 个请求；一次误熔断是**所有调用方** 10 分钟拿不到该族数据。按默认安全原则，不应让一个坏出口代表整个上游状态。
- 现实现连坏节点都不标记（见 §0 事实 2），即使不 trip，下一次还会选中它；只 trip 不换节点既保护不了可用性，也修不掉根因。
- 有界条件（防止把保护改成无限重试）：只允许 1 次换节点；第二次 reset（不同节点，或直连/静态代理下第一次）立即 trip；冷却维持 600s；非 push2 路径行为不变。

落地约束：这是 QUOT-370 第 2 条的「技术选型」，需项目负责人/老板批准后才改代码；本单只定语义，不改实现。

## 2. 限流退避与端点族熔断叠加时的行为

**结论 A：退避等待计入熔断窗口。**

熔断窗口是 trip 时刻写下的单调时钟绝对截止时间（`until = monotonic() + 600`，`core/http.py:174-177`），不区分时间花在请求上还是花在客户端睡眠上。客户端按 `retry_after_s` 睡满后重试时，这段睡眠真实流逝，自然消耗冷却余量。只有「新的 reset 再次 trip」才会把截止时间重置为 now+600；退避本身不会延长窗口。

**结论 B：熔断期间退避不累积。**

- 服务端：熔断打开时 `fetch` 在 `endpoint_limiter.acquire()` **之前**就抛 `QFSD-7001`（`core/http.py:423-436`），期间没有请求打到上游，也不占用 25s/60s 槽位；60s 窗口在 600s 冷却结束前早已清空，恢复时从干净窗口起步。
- 客户端：`_Backoff` 是「一次取数」内的预算，跨轮新建；health 冷却按源覆盖写（`set_cooldown`），不是相加。600s 的建议时长大于 `max_wait_sec=15`，LadderStock 不会等待、直接降级/回放，不会把整轮定时任务拖住。
- 边界：只有熔断剩余 ≤15s（临近到期）时客户端才会等并重试；等完恰好过期属于预期恢复路径。`retry_after_s = int(remaining) + 1` 自带 ≤1s 富余，不会在窗口内提前打上游。

## 3. 坏节点换节点重试是否计入 60 秒窗口计数

**定论：计入 60 秒窗口次数（3 次/60s），但不重新触发 25s 最小间隔。**

理由：

- 60 秒窗口约束的是**真实打到上游的请求预算**，这是防封 IP 的底线；25s 最小间隔约束的是「调用发起节奏」。两者目的不同。
- 若重试不计入窗口：失败风暴下最坏是 3 次调用 × 每次 3 次尝试 = **9 次真实请求/60s**，正好在 429/5xx 时最容易把限流打成封禁。现实现把 429 与 5xx 一样盲重试（间隔 1s、2s，`core/http.py:476-477, 500-501`），没有读 `Retry-After`。
- 若重试也走 25s 最小间隔：换节点会先被自己闸门拦 25s，failover 失去意义，只能降级。所以重试必须免最小间隔、但占窗口配额。

现状差距：实现不满足本定论（acquire 在循环外，重试不占窗口）；429 也应交给 LadderStock 退避而不是在 `fetch` 内盲重试。列为后续实现项，本单不改代码。QUOT-370 的 `attempts` 计数（真实往返，含重试）正好可以用来验收「窗口内真实请求数」。

## 4. 已知缺口（本单只记录，不修）

1. **熔断/闸门是进程内状态**：`ENDPOINT_BREAKER`、`_ENDPOINT_LIMITERS` 都是内存对象，MCP 主进程与 crawler 子进程各一份；健康文件只共享只读计数，不共享熔断状态。因此上游实际承受的预算约为「进程数 × 3 次/60s」。秒级方案必须按**所有进程合计**折算，不能按单进程算。
2. **429 盲重试**：见 §3，建议 429 不重试，直接带 `retry_after_s` 返回给 LadderStock 退避。
3. **reset 不标坏节点**：见 §1，与有界 failover 一起修。
4. **两套 reset 判定**：`_is_connection_reset`（原始异常，`fetch` 用）与 `is_connection_reset`（解包 `TickHubError` 用）不要跨层混用，否则会出现「被包装后不再 trip」或「HTTP 状态误判成 reset」。

## 5. 对 QUOT-407「秒级压测」的输入

- 压测只能测「单出口、单进程」的 ulist.np 单路径，结论外推到生产时要乘上进程数/出口数，并留安全系数。
- 在压测结论出来前，闸门维持 25s / 3 次每 60s / 600s 不变；任何秒级上线方案不得绕过本节 §3 的窗口口径。
- 压测脚本的中止判定（「连接重置/中断」）必须按异常类型 + `winerror/errno` 识别，不能复用英文消息匹配——原因见 §6。

## 6. 补记：reset 判定在中文 Windows 上漏判（实测，影响 §1 / §3 / §4）

`_is_connection_reset()` 先用类型过筛，再用 6 个英文 marker 匹配异常消息文本（`core/http.py:195-209`）。2026-11-05 在本机（zh-CN Windows，Python 3.12）做了纯本地 socket 实验（不访问任何上游）：

- 服务端强制 RST 后，客户端抛 `ConnectionResetError(10054)`，`str(exc)` = `[WinError 10054] 远端主机强迫关闭了一个现有的连接。`（中文）；
- `_is_connection_reset(原始异常)` = **False**——类型已经证明是重置，却因消息里没有英文 marker 被拒；
- 包成 `requests.exceptions.ConnectionError`（`fetch` 实际会看到的形态）后仍为 **False**；
- 同类异常在英文 locale 下消息含 `connection reset`，才会判 True。

影响：

1. 在这类机器上 `fetch` 不会 trip 端点族熔断（`core/http.py:488`），QUOT-370 的 `resets` 计数也不会增加——§1 的「有界 failover」同样无从触发。语义定得再对，判定漏了等于没有保护。
2. 公开版 `is_connection_reset()` 对裸异常按类名可判对（`ConnectionResetError` ∈ `_RESET_CLASSES`，`core/http.py:212-231`），但 `fetch` 最终抛的是 `TickHubError`，`detail["error"]` 记的是 `requests` 包装类 `ConnectionError`（`core/http.py:511`，不在集合内），对本地化消息同样兜不住；两套判定在这里会给出不同答案。
3. 旁证：QUOT-370 的离线演练在中文 Windows 上抛 `ConnectionAbortedError(10053)`（本地化消息）时同样未被判为重置（PR #3 评论，2026-11-04）。

实现约束（与 §1 同批落地，属 QUOT-370 第 2 条的实现范围，本单不改码）：

- 判定以「异常类型 + `winerror/errno`」为准（Windows 10053/10054，POSIX ECONNRESET=104），消息 marker 只作最后兜底；
- `detail["error"]` 应保留原始 socket 异常类名或 winerror，避免被 `requests` 包装后丢信息；
- 回归用例用「本地化消息 + 原始类名/errno」构造，不要写成依赖英文文本的断言；
- 修复前，凡依赖「reset→熔断」的容量假设（含秒级方案）在 Windows 部署上都不成立，需按未熔断的最坏情况折算上游预算。
