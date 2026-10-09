# P0001.17 §11/§12：五大 Surface 状态审计（真实数据 + 真实浏览器验证）

审计对象：`ui/pages/{monitor,market,activity,performance,system}` + console shell（`ui/client/console.js`）。
证据来源：`tests/ui/local_paper_demo.py`（真实本地 PAPER 闭环 runtime）+ `tests/ui/capture_local_loop.mjs`
（真实 Chrome + CDP，断言文本与 API 往返），结果存档于本目录 `local_loop_capture.json`。

## 全局 shell（所有 Surface 共用）

| 状态 | 行为（真实证据） |
| --- | --- |
| Loading | `renderSurface()` 在 `await module.render()` 前写入 `<h2>loading</h2>`（真实 DOM 状态，非占位组件） |
| Error | 页面渲染/请求失败 ⇒ `error` 段显示异常文本 + **恢复入口**（Monitor / System health / retry this page + 针对 run/decision 错误的说明）。实测：`#/market/run-review/run-does-not-exist` 走 404 分支并显示恢复说明 |
| Snapshot 失败 | header banner 显示 `snapshot unavailable: …`，blockers 显示 `UNKNOWN`（不伪装健康） |

## Monitor（"现在系统怎么样？"）

- Loading：见全局 shell；snapshot 加载期间显示 `loading`。
- UNKNOWN：prediction/strategy/risk/readiness 等无事实时显示 `UNKNOWN(reason)`；本页新增 `prediction source` 行在无记录时为 UNKNOWN。
- Empty：无挂单时 `orders 0`（真实 0，不是空）——0 与 UNKNOWN 可区分。
- LOCAL_TRIAL：真实显示 `LOCAL_TRIAL · 本地试验规则，非真实模型`（远程 run 实测命中）。
- 导航：blockers → System 对应 section；Surfaces 导航链接齐全。

## Market（"市场发生了什么？"）

- Loading：全局 shell；图表数据异步加载时工具条/图表区保持 loading 段。
- Error：`/api/v1/runs/<id>/market` 404 ⇒ 显示 `UNKNOWN (HTTP…)`；`#/market/live` 请求失败走全局 error 段。
- Empty：无 K 线（未接线 market 缓冲）时显示 UNKNOWN 说明，不画假图。
- **图表数据链**：实测 181 根真实 1m K 线（API 与图表同源，末收 61095.4）；指标 **MA/EMA/VOL 由成熟图表库（klinecharts）计算**。
- **VWAP / ATR：NOT_AVAILABLE（真实原因）** —— 后端**没有**这两个事实：`FeatureEngine` 不消费 TRADE 事件
  （实测 `trade_stream_available=False`、`trade.vwap=None`），且不存在 ATR 实现。按人类裁决 §5
  「不要在 UI 重新计算业务指标」，本轮**移除了 UI 侧自算的 VWAP/ATR**，页面显式显示
  `NOT_AVAILABLE + reason`（守卫测试 `tests/unit/test_chart_indicators.py`）。
  > 更正记录：本文件早期版本曾写"VWAP/ATR 由 FeatureEngine 事实接入"，该表述**不成立**，已更正。
  若后续要让 VWAP 可用，需让 FeatureEngine 消费 TRADE 事件（会改变 MarketState/状态指纹语义）——
  属新架构变更，未获授权，故未实施。
- **语义叠加**：决策/订单/成交事实经有界缓冲进入 overlays（实测 47 decisions / 113 executions）；浏览器实测
  K 线上 **354 个 semantic markers**，与价格同时间轴。

## Activity（"系统做了什么？为什么？"）

- Loading/Error/Empty：全局 shell；无 trace 阶段时显式 `ABSENT + 原因`（不静默跳过）。
- UNKNOWN：缺阶段显示 ABSENT/UNKNOWN 并给出 reason（如 prediction 未接线）。
- **因果链 drill-down**：点击 decision id 展开 market state hash / prediction（含 LOCAL_TRIAL 标记）/ decision(action+reason) / risk / orders(含 venue_order_id) / fills / accounting；真实数据实测有内容。
- 跳转：order/fill 行提供 `market @ <ts>` 链接（跨页定位）。

## Performance（"做得怎么样？"）

- 真实 account timeline 驱动 equity/PnL/exposure（实测 231–232 点，非 mock）。
- **Trade statistics（§8）**：展示真实事实（窗口内成交笔数 / 手续费 / realized / unrealized / equity / drawdown）；
  `win/loss counts` 与 `average pnl per trade` 显式 `NOT_AVAILABLE` + 原因（读模型没有逐笔平仓 PnL）。
- 无数据时显示 UNKNOWN 原因（timeline unavailable 字段透传），不用 0 冒充。
- Run selector → Run Review 跳转；跨页携带 run。

## System（"系统本身是否健康？"）

- 两个 connector health **分开**显示（market / private），无单一 `connected`；实测两段标签均命中。
- 未观测到私有事件时显示 `未观测到业务事件（≠ 没有成交）`（UNKNOWN ≠ 空）。
- 执行审计（last submit classification/reason）显示真实原因或 UNKNOWN。
- 配置/能力/ops 段：未接线项显示 UNKNOWN + reason。

## 跨页上下文一致性（§12）

- `ui/client/selection.js` 是**唯一**选中 store；Assistant 通过 `ui/assistant/context.js` 读取同一 store
  （修复前 drawer 把选中嵌套传递导致后端收不到；现已展平并实测命中）。
- 进入 Market（live/replay/run-review）与 Orders 时写入 `run/symbol/timestamp/order` 并**清空** `decision/order/fill`，
  避免串 run；Activity 选中 decision 时写入 `decision` 与当前 `run`。

## 逐页状态**注入验证**（P0001.17 §2/§16B，真实故障注入 + 真实空数据）

测试资产：`tests/ui/fault_server.py`（两种模式）+ `tests/ui/capture_states.mjs`（真实 Chrome）。
结果：`surface_states.json`（与本文件同目录）。

| Surface | fault 模式（provider 真实抛错 ⇒ 503） | empty 模式（真实空 event store） |
| --- | --- | --- |
| Monitor | error 文案 + **恢复入口**（Monitor / System health / retry）+ UNKNOWN+reason ✔ | `health=UNKNOWN`、`data_ts=UNKNOWN (no market data consumed yet)`（带 reason）✔ |
| Market | 同上（error + recovery）✔ | K 线/事实全 UNKNOWN（不画假图）✔ |
| Activity | 同上 ✔ | 各 canonical 阶段显式 ABSENT/UNKNOWN + reason ✔ |
| Performance | 同上 ✔ | equity timeline UNKNOWN（带原因），不显示 0 ✔ |
| System | 同上 ✔ | connector/execution facts UNKNOWN（不显示 connected/健康）✔ |

- fault 模式真实响应：`/api/v1/snapshot -> HTTP 503`、`/api/v1/market -> HTTP 503`（provider 抛 `InjectedFailure`）；
  header banner `snapshot unavailable: …`、blockers `UNKNOWN`、页面 error 段含 recovery 链接。
- empty 模式：事件存储为**空文件**（真实无数据，不是 mock）⇒ 全部状态为 UNKNOWN/ABSENT 且**都带 reason**；
  5/5 Surface 导航仍可用（`navOk=true`）。
- 两模式 5/5 Surface 均通过（`errHint` / `unknown+reason` / `nav`），fault 模式另含 5/5 recovery。

## 浏览器 harness 的已知采样限制（如实记录）

`capture_local_loop.mjs` 的 surface 断言是"navigate 后固定 settle 再读文本"。在机器负载高时，
个别页面（本轮出现一次 `05-system-connectors`）可能在采样点尚未渲染完 ⇒ 该次断言 missing 非空。
已单独复核：`#/system/connections` 的 `market data connector` / `private execution connector`
标签均真实存在（直接读取页面文本验证）。后续可把固定 settle 改成"等待标志文本出现"。

## 两项限制已消除（2026-10-04，人类「全部做掉」）

1. **历史 run 的图表现在是真实 OHLCV（不再是 mid-only）**：新增 `runs/<id>.trades.jsonl`
   （真实逐笔：ts / price / quantity / aggressor，由既有 `RunRegistry` Owner 追加），
   `/api/v1/runs/<id>/market` 用**持久化逐笔 + 记录盘口**在服务端聚合 ⇒ 181/181 根 K 线带真实成交量、
   逐桶 VWAP 与服务端 ATR(14)。实测：`trades persisted=15445`、`candles=181`、`with volume=181`、
   `atr non-null=168`、`source=trades`、`note="OHLCV from persisted trades"`。
   UI 同步显示"N 笔持久化成交 / 由真实成交聚合"；无逐笔的 run 仍如实显示 `UNAVAILABLE`。
2. **浏览器 harness 改为条件等待**（不再固定 settle）：`capture_local_loop.mjs` /
   `capture_states.mjs` / `verify_chart.mjs` 均等待标志文本或 chart API 真实出现（上限内不出现即报 missing）。
   复验：local loop 全部 Surface `missing=[]`、Assistant 选中回答通过、chart verify PASS
   （181 candles / 437 markers / zoom-pan / timeframe / replay sync）；fault 5/5（含恢复入口）、
   empty 5/5（UNKNOWN + reason）。
