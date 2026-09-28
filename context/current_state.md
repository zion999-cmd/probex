# Current State

## 当前阶段

P0001.1 – P0001.4 已完成并通过全部验收。
**P0001.4.1（Real Jev Transport Validation）已完成**，结论（人类 2026-09-28 决策，D-017）：
`OpenRouterTransport` **VALIDATED**；`OpenRouter typesafe/jev-router` 作为热路径 Jev Provider **REJECTED_FOR_NOW**
（unstable model identity / unstable output contract / 4.9–19 s latency）。
P0001.4.1 以 `CONTRACT_MISMATCH` / `PROVIDER_UNSUITABLE` 作为有效实验结论关闭，SC-1 按决策豁免，`jev-market-v1` 保持不变。

Provider identity 核实（决策 D，提案 §1.7）与授权探测 P1/P2（提案 §1.8）已完成，结论 **NATIVE_TYPED_JEV_AVAILABLE**（D-018）：
- 旧 ~270 ms Jev = 同一 OpenRouter 平台的 `POST https://openrouter.ai/api/v1/systemone` + model `jev-1.13`
  （解析为 `typesafe/jev-1.13-20260917`，provider `TypeSafe`），typed `answers.<id>.{noul|choice|score}`、不生成文本。
- 授权探测：P1 中 `typesafe/jev-router` 被标记为 **Router**（`tokenizer=Router`、`pricing=-1/-1`），`typesafe/jev-1.13` 不在 chat 模型清单；
  P2 单条 `noul` 探针 → HTTP 200、604 ms、`answers.ok={type:noul,noul:0.99}`、cost 1.1634e-05。
- **热路径 Jev 的真实目标应为 `/api/v1/systemone`**；实现 typed provider 需新提案（尚未授权、尚未改代码）。

`context/status.json` 的 `currentProposal` 为 `null`（**P0001.9.5 已收口**，等待下一条正式 Proposal）。

## 已完成能力

### P0001.1 — Market Event + L2 Book + BookHealth

统一 `MarketEvent` 边界（8 字段）、`BookSnapshot` / `BookDelta` 载荷不变量、Binance USDⓈ-M 深度归一化（边界 fail closed）、`OrderBook`（档位增删改、best、top-N、序号四判定）、`BookHealth` 四态状态机 + `MarketBook` 重同步闭环 + `is_tradeable` 门。

### P0001.2 — Event Store + Deterministic Replay

append-only JSONL Event Store（接口与 adapter 分离）、strict 递增 ordinal、canonical JSON、内容寻址 `event_id`、损坏输入 fail closed、`ReplayClock`、`ReplaySource`（FULL / STEP）。

### P0001.3 — MarketState + Microstructure Feature Engine

immutable `MarketState`（schema `market-state-v1`）、price / depth / flow / trade / returns / volatility 六段 feature、event-domain OFI、多窗口收益与每秒已实现波动率、`DataQuality` 闸门、windows 基础设施（`TimeSeries` / `TimeWindow` / `EventWindow`）、盘口失健康时 feature 全部显式 unavailable。

### P0001.4 — Jev Prediction Runtime

- `prediction/schema/market_v1.py`：question schema `jev-market-v1`（`future_return` 4 horizons × 5 buckets + 4 个单概率问题）、canonical Jev payload、`market_state_hash`。
- payload 只含市场信息（字段白名单 + 禁止账户 / 仓位 / 盈亏 / 风险字段）。
- `PredictionRequest` 冻结输入（payload 以 canonical JSON 字符串落盘）；`Prediction` / `PredictionRecord` 不可变。
- `PredictionProvider` protocol + `JevProvider`（传输注入式 adapter）+ strict parser（概率范围、分类齐备、求和解、NaN / Infinity / 未知字段全部 fail closed）。
- `PredictionScheduler`：资格闸门（与 `DataQuality.tradeable` 等价并给出原因）、limited inflight（默认 1，不排队）、stale response rejection。
- `PredictionRuntime`：async 调用与 Market Feed 解耦、timeout、exponential backoff 与 provider 状态（HEALTHY / DEGRADED / BACKING_OFF）、TTL 一等字段与 expired 判定、provider/model/raw response 证据。
- `RECORDED` / `LIVE_REQUERY` 两种 Replay 语义：RECORDED 只查历史记录、不调用 provider、不触网。
- 全部测试使用 `tests/fakes.py` 的 `FakeClock` / `FakeProvider`，无网络、无 Key、确定性。

### P0001.4.1 — Real Jev Transport Validation（已完成，实验结论）

- `OpenRouterTransport`：**VALIDATED**（外层契约 / HTTP 状态映射 / timeout / telemetry / Key 隔离）。
- `typesafe/jev-router` 作为热路径 Jev Provider：**REJECTED_FOR_NOW**（model identity 不稳定：实测解析为
  `stealth/space-bunny-alpha`；输出契约不稳定；latency 4.9–19 s）。
- P0001.4.1 以 `CONTRACT_MISMATCH` / `PROVIDER_UNSUITABLE` 作为有效实验结论关闭；`jev-market-v1` 未改动。

### P0001.4.2 — Native Typed Jev Provider（已完成）

- 热路径改为 native typed System One：`POST https://openrouter.ai/api/v1/systemone`，model alias `jev-1.13`。
- `prediction/systemone_wire.py`（端点 / question id / Choice+Noul 构造 / state / 请求体）、
  `prediction/providers/systemone.py`（`SystemOneTransport` + `SystemOneProvider`）、
  `prediction/parsing/systemone.py`（typed answers → `jev-market-v1` domain answers）。
- 五分类未来收益 = 原生 **Choice**（`market_5s|15s|30s|60s`）；adverse selection / fill = 原生 **Noul**；
  Noul 无 confidence 且永不伪造；`provider_confidence` 取最近 horizon Choice 的 confidence。
- `PredictionRecord` 追加 `requested_model` / `resolved_model` / `response_id` / `usage`（向后兼容）。
- 既有 `Prediction` / `PredictionRecord` / TTL / Scheduler / Archive / Backoff / stale 语义**未改**；
  typed answers 经 domain JSON 交给**既有** strict parser。
- Chat Completions 热路径正式废弃（模块保留为 P0001.4.1 实验记录，已从包命名空间移除并标记 `DEPRECATED`）。
- 真实实测（3 次请求）：latency 409/462/413 ms、resolved `typesafe/jev-1.13-20260917`、provider `TypeSafe`、
  cost ≈1.1e-04/call、五分类求和 = 1、Noul 无 confidence。

### P0001.5 — Accounting + Risk Core（已完成）

- `portfolio/`：`Fill`（不可变事实）+ `FillLedger`（`(venue,symbol,fill_id)` 与 `(venue,symbol,trade_id)` 双键去重）、
  `Position`（净持仓；加仓 / 部分与全部平仓 / 反手拆成 close + open leg）、`FundingLedger`、
  `AccountingCore`（账户状态唯一 Owner；`balance` 由账本派生，`equity = balance + unrealized`；
  `realized_trade_pnl` / `trading_fees` / `funding` 三者分开）。
- `risk/`：`OrderProposal`（输入契约，不是 Order）、`RiskSnapshot`（gate 唯一消费的不可变快照）、
  `RiskLimits`、`RiskGate`（position / notional / open-order exposure / available balance / daily loss /
  drawdown / leverage / liquidation distance / market data / kill switch）、`RiskDecision`（含 `reason_code`）。
- reduce-only 语义：真正降暴露只受硬检查约束；会增大暴露则 `REDUCE_ONLY_WOULD_INCREASE`。
- 未知 ≠ 0 与 fail closed：缺 mark / 缺当日盈亏 / 缺强平信息 / 已配置限额但数据缺失 → REJECT。
- 时间边界由调用方注入（`now_ms` / `day_start_ts`）；`portfolio/**`、`risk/**` 不使用 wall-clock。
- 确定性：同组 Fill/Funding/Mark 重放逐字段一致；重复交付（每笔两次）结果不变。

### P0001.6 — Order Lifecycle + Paper Execution（已完成）

- `execution/types.py`：`Order`（不可变快照）、`OrderStatus` 九态 + 转换表、`ExternalOrder` / `ExternalFill`。
- `execution/events.py`：六类 `ExecutionEvent`。
- `execution/tracker.py`：`OrderTracker` —— 本地状态权威；双键成交去重；cancel-pending；LOST；late fill；
  active / recent_terminal / lost 视图；`open_order_exposure`（未成交部分按订单价格）。
- `execution/adapters/{base,paper}.py`：`ExecutionAdapter` 契约 + `PaperBroker`（可控 accept/reject/fill/
  duplicate/late/cancel-defer/status-update/drop-from-external + post-only 基本规则）。
- `execution/reconciliation.py`：`reconcile(...) → ReconciliationReport(actions, converged)`（四类情形 + fail-closed 分支）。
- `execution/manager.py`：`OrderManager`（submit / cancel / **cancel-before-replace**）。
- `execution/engine.py`：`ExecutionEngine`（新鲜 RiskSnapshot → RiskGate → submit；事件 → tracker → canonical Fill → Accounting）。
- `risk/`：kill switch 三态 `NORMAL / REDUCE_ONLY / HALT_ALL`（HALT_ALL 禁 submit、允许 cancel）。
- 纪律：cancel request ≠ cancel success；终态不可回退；late fill 只更新成交事实；Execution 不 import Prediction/Jev/Strategy。

### P0001.6.1 — Uncertain Order Exposure（已完成）

- 暴露三视图：`confirmed_open_exposure`（ACTIVE）/ `uncertain_exposure`（LOST）/ `total_pending_exposure`；
  `RiskSnapshot.open_order_exposure` 使用总量。
- 资料不足的订单（adopt 缺 side/quantity/price 等）→ `UnresolvedOrder`（**不按 0**）；`unresolved_order_count > 0`
  时 RiskGate 拒绝新增暴露（`UNCERTAIN_EXPOSURE_UNKNOWN`），reduce-only 降暴露仍放行。
- 释放条件：reconciliation 明确确认终态或补齐资料；仅「外部消失」不释放。
- `Order` 状态机 / Accounting / late-fill 语义均未改动。

### P0001.7 — Market Making Policy（已完成）

- 新增 `strategy/maker/`：`pricing`（QuotePrice）、`sizing`（QuoteSize）、`inventory`（InventoryBias）、
  `lifecycle`（QuoteLifecycle）、`policy`（MakerPolicy 编排）、`types`（配置与决策契约）。
- 决策形态：`MakerDecision{mode: both|bid_only|ask_only|none, bid/ask: QuoteDecision}`，
  `QuoteAction = PLACE | KEEP | CANCEL | REPLACE | NONE`；报价规格见 `MakerDecision.quotes()`。
- 全局门（fail closed）：`KILL_SWITCH(HALT_ALL|REDUCE_ONLY)` > `MARKET_UNHEALTHY` > `PREDICTION_STALE` > `UNKNOWN_EXPOSURE`。
- prediction 不可用时（缺失 / 过期 / 缺 horizon）：只禁止新增**增加暴露**的报价，**允许新增 reduce-only**，且完全退出方向性调整
  （`fresh_prediction()` 不伪造预测）—— 由 **P0001.7.1** 落地（人类裁决：降险不得被预测可用性阻断）。
- 策略层不修改 Accounting / Execution 状态机 / Prediction Runtime（SC-13 由依赖扫描测试固定）。

### P0001.7.1 — Prediction Outage Reduce-only Continuity（已完成）

- 全局门 `PREDICTION_STALE` 改为 `allow_new_reducing=True`；`MARKET_UNHEALTHY` / `UNKNOWN_EXPOSURE` / `HALT_ALL` 仍阻断全部新增报价。
- 生命周期：中断时增加暴露的旧报价 CANCEL、合法 reduce-only 报价 KEEP/REPLACE（不是全部撤掉）。
- 观察项（未实施，待裁决）：`UNKNOWN_EXPOSURE` 与 `adverse_selection_block` 对 reduce-only 的处理、中断期 REPLACE churn。
- 策略层不修改 Accounting / Execution 状态机 / Prediction Runtime（SC-13 由依赖扫描测试固定）。

### P0001.8 — Event-level Fill Simulation（已完成）

- 新增 `execution/simulation/`：`types`（QueueState / FillInferenceState / FillReason / Liquidity / SimulatedFill /
  RestingOrderView）、`queue`（队列近似）、`latency`（submit / cancel 延迟）、`fees`（显式 FeeSchedule）、
  `venue.SimulatedVenue`（实现 `ExecutionAdapter`，消费真实 Replay 市场事件）。
- `market/events` 新增 `TradePayload` + `AggressorSide`（`EventType.TRADE` 的载荷），Event Store codec 支持其编解码；
  **未**改动 `MarketBook` / Feature Engine / `market-state-v1`（`TradeFeatures` 仍 unavailable）。
- 成交证据模型：`L2 变化 ≠ 成交证据`；只有对手方向 aggressor trade 推进队列 → `QUEUE_CONSUMED` / `TRADE_THROUGH`；
  `UNKNOWN` 队列绝不产生推测性成交；盘口不可信时挂起并作废队列，恢复后必须重建。
- `PaperBroker`、Strategy、Prediction、Risk、Accounting 均未改动。

### P0001.9.1 — Binance USDⓈ-M Live Market Data（已完成，真实公网验收通过）

- `connectors/binance/market_data/`：`endpoints`（端点单一 Owner）、`transport`（标准库 WS：握手/帧/掩码/ping-pong/分片）、
  `streams`（stream 名 + tier 归属 + 订阅 + 信封）、`trades`（aggTrade → TradePayload + 水位去重）、
  `mark`（MarkPriceObservation）、`exchange_info`（TradingRules，只信 filters）、`snapshot`（REST 客户端）、
  `runtime`（两条 tier 连接、快照对齐、gap→resync、冷却、断线重连、telemetry）。
- `market/` 新增 `MarketBook.invalidate(reason)` / `FeatureEngine.invalidate(reason)` 与只读 `book_health`（SC-9 的最小机制）。
- **真实公网 Acceptance（人类本机代理隧道，无凭据）**：SC-12 PASS（REST snapshot + exchangeInfo + server time +
  depth + aggTrade + markPrice）、SC-13 PASS（45s 窗口：179 depth / 390 aggTrade / 43 markPrice、
  HEALTHY 100%、event lag 中位 −48 ms、mark age 持续刷新、telemetry 完整）。

### P0001.9.1.1 — Futures Depth Continuity Verification（已完成）

- 根因：Futures diff depth 事件**聚合**上万个 update id（median 27299/9972），`U != prev.u+1` 是常态；
  P0001.1 的窗口判据对 Futures **0% 通过** ⇒ 每条消息误判 GAP ⇒ 每秒 resync ⇒ 同步 REST 抓取阻塞读循环 ⇒ 7–36s lag。
- 修复：`BookDeltaPayload.previous_update_id`（`pu`）+ venue-aware 判据 —— 锚点用「跨过锚点」条件
  （`pu <= L < u`），锚点之后用 `pu == last`；无 `pu` 时保留 D-003 窗口规则（现货语义不变）。
- 真实流核验：`pu` 100% 连续（199/199、473 对样本）、旧规则 0%；修复后真实 smoke **gap 0 / resync 1 / HEALTHY 100% /
  event lag 中位 −48 ms**（修前 7267 ms）。

### P0001.9.2 — Private Account + User Stream Validation（已完成）

- 新增 `connectors/binance/private/`：`auth`（凭据 + HMAC 签名 + server-time offset + 遮蔽工具）、`rest`（签名 REST + listenKey）、
  `account` / `positions`（账户与持仓事实）、`events`（ACCOUNT_UPDATE / ORDER_TRADE_UPDATE / listenKeyExpired + 去重/乱序）、
  `user_stream`（7 态 listenKey 状态机 + private tier WS 客户端）、`telemetry`、`runtime`（只读运行时）、`errors`。
- 纪律：凭据只在环境变量中；签名与 query 不进日志/异常；**无任何下单/撤单端点**（静态测试固定）；
  非 one-way/USDT-M fail closed；重连后 `continuity_assumed=False`。
- 本机无凭据 ⇒ 认证 smoke NOT RUN（运行即抛 `CredentialsError ... refusing to start`，即 SC-1 的真实证据）。
- 已提交并推送：`c97d2eb`（远端 `origin/master`）。
- **真实测试网 Acceptance（2026-09-28，人类裁决用测试网）**：签名 REST（account + positionRisk）真实通过；
  `listenKey ACTIVE`；user stream 真实连接并保持；真实发生 1–3 次断开 → 重连 + `continuity_assumed=False`（SC-10 实测）；
  `private_lag_ms` 无样本 ⇒ 以 `path_rtt`（884–1139 ms < 2000 ms 阈值）为标注基准。
  真实 payload 驱动三处解析修正：`leverage` 字符串、`marginAsset` 可缺失（改由端点+USDT 资产确认）、空仓允许 `markPrice=0`（D-033）。
- **真实事件链已采集（2026-09-28，D-034 授权由实现方在测试网制造活动）**：8 条真实业务事件
  （限价 NEW → CANCELED，市价 TRADE×2 + ACCOUNT_UPDATE×2，含 commission / trade_id / reduceOnly / cumulative fill）；
  延迟 median −8 ms / p95 +2 ms / max +2 ms；`heartbeats=1`；`duplicate/out_of_order/malformed=0`；结束时空仓且 0 挂单。
  ⇒ SC-4/6/7/8/10/11/12/13 均取得**真实测试网证据**；并由此修掉第二批真实缺陷（去重键 D-035、harness 静默吞错）。
- **仍未验证**：主网（出口 IP 曾封禁 418/-1003；主网私有链路延迟与账户数据未取样）⇒ 状态串
  `TESTNET_PRIVATE_VALIDATED / MAINNET_PRIVATE_NOT_YET_VALIDATED`。
- **Risk（后续 live gate，D-036）**：raw event lag 出现 −30 ms（区间 −30 ~ +2 ms）⇒ 本地与交易所时钟有几十毫秒偏差，
  主网验收必须用 `event_lag_corrected = receive_ts − event_ts **+** clock_offset`（`offset = 交易所 − 本地`；
  2026-09-28 人类裁决更正符号）并记录不确定度；`uncertainty_ms` 单独保存。
- `status.json.currentProposal` = `null`；**P0001.9.3 不启动**，等待下一条正式 Proposal（人类 2026-09-28 指示）。

### P0001.9.2.1 — Private Connectivity Contract Audit & CCXT Fit（已完成；裁决 KEEP_NATIVE_PRIVATE）

- 代码修正（已验收）：`ORDER_TRADE_UPDATE` symbol 只取 `o.s`；listenKey REST 异常一律离开 `RENEWING`（HTTP 失败 ⇒ `FAILED`）；
  `stop()` best-effort 幂等（DELETE 失败仍 `STOPPED`）；`stream_connected_at_ms` = 真实 WS 连接时刻 + 新增 `snapshot_started_at_ms`。
- 审计（只读、无凭据）：`wss://ws-fapi.binance.com/ws-fapi/v1` + `userDataStream.start` 实测存在（无效 key ⇒ 401/-2014）；
  private tier URL `wss://fstream.binance.com/private/ws?listenKey=` 与 ccxt 4.5.84 完全一致（无 `events=`）；
  `events=` 不是连接准入条件，真实投递仍需有效 listenKey（待 P0001.9.2 真实 smoke）。
- CCXT Pro fit（仓库外 PoC）：`watch_orders/watch_my_trades/watch_balance/watch_positions` 齐备；
  `order['info']`/`trade['info']` **保留原始 payload**（Probex 所需 raw 字段可取）；但 `parse_position` **NotSupported**、
  归一化字段不足需读 `info`；ccxt 对 user data **无任何连续性证据** ⇒ `continuity_assumed` 与 P0001.9.3 不可省。
- **人类裁决（2026-09-28）：`KEEP_NATIVE_PRIVATE`** —— 不引入 `ccxt`/`ccxt.pro`，不改 public market data；
  继续使用 native private transport（见 D-032；D-031 的待裁决项已关闭）。

### P0001.9.3 — Startup Recovery + Account Reconciliation（已完成）

- 新增 `connectors/binance/private/orders.py`（`ExternalOrder` 归一化 + ownership boundary）、
  `trades.py`（`ExternalFill` 归一化，`orderId → clientOrderId` 映射）、
  `recovery.py`（`StartupRecovery` 编排 + `RecoveryGate`：`RECOVERED` / `BLOCKED` + reason code 全表）；
  `rest.py` 追加三只读签名 GET（`open_orders` / `order_history` / `user_trades`，均为 `GET`）。
- `portfolio/`：`ExternalAccountBaseline` + `AccountingCore.bootstrap_from_baseline(...)` —— **一次性** startup baseline，
  **不产生 synthetic Fill**；baseline 之后历史 PnL / 峰值保持 UNKNOWN（`net_realized_since` 早于水位线 → `None`，
  `peak_equity` / `drawdown` → `None`），`RiskGate` 新增 `MISSING_DRAWDOWN` 继续 fail closed（D-037）。
- 恢复门（全有才 `RECOVERED`，任何未知即 `BLOCKED`）：stream `ACTIVE` + `continuity_assumed` + boundary 有效、
  account/position/openOrders/allOrders/userTrades 读取成功、无 foreign 未平挂单、无 unresolved 订单、
  无 `STATUS_CONFLICT` / `ADOPT_REJECTED`、baseline 与新鲜持仓复核一致、one-way / USDT-M / symbol 契约成立。
- 断线纪律：`invalidate()` ⇒ 立即回落 `NOT_RECOVERED`；**自动重连成功也不得自动 RECOVERED**，必须重走完整流程。
- **测试网真实验收 PASS（2026-09-28）**：`RECOVERED`、`reasons=[]`、probex open 0 / history 5 / fills 2、
  foreign open 0、baseline_applied true、**synthetic_fills 0**、`historical_pnl_known=false`（真实报告见提案 §1.2）。
- 真实数据驱动的修正：空仓时 Binance 仍返回真实行情 `markPrice`（非 0）⇒ baseline 规则改为「空仓允许 mark>0 且
  `entryPrice` 必须为 0；有仓 mark 必须 >0」；并窄修 `reconciliation._apply_status` 缺失 `avg_fill_price` 的既有缺口。
- 全量测试 **1387 passed / 0 failed / 19 skipped**；`context/status.json.currentProposal` 回到 `null`。

### P0001.9.3.1 — Recovery Contract Closure（已完成）

修复 P0001.9.3 审计发现的三处契约缺口（不含新能力）：

- **严格 ownership**：`OWNED_CLIENT_ORDER_ID_PREFIX = "probex-"`（前缀 + 至少一个字符）；
  `probexevil-1` / `probex2` / `probex` / `probex-` 全部归为 external（碰撞前缀的未平挂单 ⇒ `FOREIGN_OPEN_ORDER`）。
- **成交三分类**（替换原两分类）：自己的 → reconcile；已验证外部 → `foreign_fills` 计数；
  **不可归属** → `BLOCKED: UNRESOLVED_FILLS`，判定发生在 reconcile/baseline **之前**（阻塞时不改账本、不 adopt）。
- **断线自动失效真实接线**：`PrivateAccountRuntime.subscribe_discontinuity(listener)`（断线重连 / listenKey 重建 /
  `stop()` 广播 + `discontinuity_events` 审计）；`StartupRecovery.bind(runtime)` 订阅后自动回落 `NOT_RECOVERED`；
  测试不再手工调用 `invalidate()` 模拟接线。
- **真实测试网证据（SC-3）**：10 单中 5 自有（`probex-audit-*`）/ 5 外部（`web_*`）；7 笔成交 → 自有 2、
  已验证外部 5、**不可归属 0** ⇒ `RECOVERED`（`foreign_fills=5`、`unresolved_fill_order_ids=[]`、`synthetic_fills=0`）。
- 全量测试 **1405 passed / 0 failed / 19 skipped**；`context/status.json.currentProposal` 回到 `null`。

### P0001.9.3.2 — Discontinuity Notification Reliability（已完成）

- `_notify_discontinuity` 升级为**故障隔离的 observer 边界**：先记录 `discontinuity_events` 事实 →
  逐个 listener 独立 `try` → 异常**绝不外抛**（只捕获 `Exception`；`KeyboardInterrupt` 等照常传播）
  ⇒ listener 崩溃不会破坏 reconnect / listenKey recreate / `stop()`。
- 失败审计沿用 counter 风格：`discontinuity_listener_failure_count` +
  `last_error = "discontinuity listener failed: <ExceptionType>"`（**只记类型名**，不记消息/参数/堆栈 ⇒ telemetry 无敏感旁路）。
- 新增 `DiscontinuityNotificationReliabilityTest` 11 条（多 listener 故障、reconnect / recreate / stop 回归、
  审计与不泄漏、`BaseException` 不被吞）；**未改** recovery 业务语义 / ownership / fill 分类。
- 文档状态清理（SC-6）：`current_state` 版本表改为 `4b69a73` 并明确已提交 push；`handoff` 顶部新增历史段落
  **superseded** 说明，两处「尚未 commit」表述就地标注 superseded。
- 全量测试 **1416 passed / 0 failed / 19 skipped**；`context/status.json.currentProposal` 回到 `null`。

### P0001.9.4 — Live Readiness Gate（已完成）

- 新增 `readiness/`：`types.py`（`LiveReadinessStatus` / `LiveReadinessScope` / 15 个 reason code /
  `ReadinessPolicy` / `LiveRiskPolicy`，全部必填无默认值）、`gate.py`（`LiveReadinessGate`，纯判定、收集全部原因）、
  `evidence.py`（telemetry / account snapshot / `RiskSnapshot` → 证据的纯映射）。
- **`RECOVERED` ≠ `LIVE_READY`**：readiness 独立于 `RecoveryStatus` 与逐订单 `RiskGate`；`LIVE_READY` 带作用域
  （testnet 只能得 `TESTNET_LIVE_READY`；主网缺验证证据 ⇒ `MAINNET_PRIVATE_NOT_VALIDATED` ⇒ BLOCKED）。
- **修正 drawdown 事实链**：baseline 后 `peak_equity is None` ⇒ `drawdown/drawdown_pct` 为 `None`
  （不再用当前 equity 冒充峰值）⇒ 已配置限额时 `RiskGate: MISSING_DRAWDOWN` fail closed。
- **live 可用余额取交易所事实**：`RiskSnapshot.available_balance` 支持 Binance `availableBalance`
  （`BINANCE_ACCOUNT_SNAPSHOT` + `captured_at` + `age_ms`）；Paper/Replay 仍为本地推导（`LOCAL_DERIVED`）。
- **live 风险策略显式化**：`LiveRiskPolicy` 全字段必填并可 `to_limits()` 喂给 `RiskGate`；
  缺失 ⇒ `RISK_LIMITS_NOT_CONFIGURED`；kill switch 非 NORMAL ⇒ `KILL_SWITCH_NOT_OPERABLE`。
- **D-036 落地**：`ClockCalibration`（offset / RTT / uncertainty / measured_at）；runtime 主延迟指标改为校正值
  （原始值仅审计）；未测量/过旧 ⇒ `CLOCK_NOT_CALIBRATED`，uncertainty 超阈值 ⇒ `CLOCK_UNCERTAINTY_TOO_HIGH`。
- **真实测试网只读验收（SC-12）**：`recovery = recovered` 而 `readiness = blocked`，原因
  `PRIVATE_LATENCY_UNKNOWN`（窗口内无业务事件）/ `HISTORICAL_DAILY_PNL_UNKNOWN` / `HISTORICAL_DRAWDOWN_UNKNOWN` /
  `MARKET_NOT_READY`（本 harness 未运行 public market-data 链，未伪造 green）；真实 clock offset 199 ms /
  RTT 292 ms / uncertainty 146 ms；交易所 availableBalance 4998.72604447；`synthetic_fills = 0`。
- 全量测试 **1474 passed / 0 failed / 20 skipped**；`context/status.json.currentProposal` 回到 `null`。
- **实现修正（2026-09-28，人类裁决）**：D-036 时钟校正公式符号原本写反，已改为
  `corrected = receive_ts − event_ts + offset_ms`（= `raw + offset`），并加入符号方向单测
  （`offset=+200/raw=−180 → 20`、`offset=−100/raw=+130 → 30`、`offset=0/raw=25 → 25`）。

### P0001.9.4.1 — Historical Risk Bootstrap（已完成）

- 新增 `connectors/binance/private/income.py`（只读 `/fapi/v1/income` 事实：严格解析、显式 incomeType 分类、
  `(income_type, tranId)` 去重与冲突检测、结算资产校验、**完整分页**（`max_pages` 显式、超页/越界 ⇒ coverage 不完整））
  与 `connectors/binance/private/rest.py::income_history(...)`（账户级、不传 symbol）。
- 新增 `risk/history.py`：`HistoricalRiskBaseline`（`daily_net_realized=None` = 未知；
  `drawdown_known`/`peak_equity_known` **恒 False**）+ `compose_daily_pnl(...)`；
  `risk/snapshot.py` 支持 `historical_baseline=` 注入 ⇒
  `realized_pnl_today = Σ(trading income, time ≤ cutoff) + accounting.net_realized_since(cutoff)`（不双计）。
- 三件事实分离（daily / drawdown / peak）；**绝不**从 income 或当前 equity 反推历史 peak（D-044）。
- **真实测试网只读验收（SC-16）**：coverage complete、1 页 11 行、trading 10（COMMISSION×7 + REALIZED_PNL×3）、
  non-trading 1（TRANSFER）、unclassified 0、**daily_net_realized = −1.27395553 USDT**、全部 USDT 资产。
- **真实 readiness 变化（SC-13）**：`HISTORICAL_DAILY_PNL_UNKNOWN` **被真实解除**，
  剩余阻塞为 `PRIVATE_LATENCY_UNKNOWN` / **`HISTORICAL_DRAWDOWN_UNKNOWN`** / `MARKET_NOT_READY`。
- **implementation correction（2026-09-28，人类裁决）**：去重身份按"是否参与 PnL"分开 ——
  TRADING 行严格用 `(income_type, tran_id)`（冲突 ⇒ BLOCKED）；NON_TRADING / UNCLASSIFIED 行只做审计层去重
  （`TRANSFER.tranId=0` 可多条共存，不影响 daily PnL）；incomeType 白名单**不扩展**（已知非交易只有 `TRANSFER`）。
- 全量测试 **1515 passed / 0 failed / 22 skipped**；`context/status.json.currentProposal` 回到 `null`。

### P0001.9.4.1.1 — Full Testnet Readiness Integration Validation（已完成）

- 新增 harness 侧映射 `tests/live/market_readiness.py`：把**真实 public 事实**（`book_health` / 快照锚定 /
  窗口内 gap·resync·malformed / mark age / feed age / aggTrade 数量）映射为 `market_ready`（**不再硬编码 bool**）+ 测量纪律
  （窗口起点在初始锚定之后；锚定用绝对事实）。
- 新增 opt-in 全链 live 测试与仓库外 runner（`/tmp/probex_live/{min_order_activity,run_full_readiness_testnet}.py`）：
  public → private → income → recovery → **授权边界内的最小订单活动**（LIMIT GTX → NEW → 撤销 → CANCELED）→ 再 recovery → readiness。
- **真实结果（同窗口全链）**：`market_ready=true`（锚定 ✅、窗口内 gap/resync 0、aggTrade 92、mark/feed 501 ms）；
  corrected latency 样本 2 条（157/164/171 ms，raw −104/−97/−90，**corrected − raw = 261 = offset**）；
  clock offset 261 / RTT 342 / uncertainty 171 ms；income complete + `daily_net_realized = −1.27395553 USDT`；
  活动前后 recovery 均 `RECOVERED`；最终 position 0、open orders 0；
  **readiness = `blocked`，唯一原因 `HISTORICAL_DRAWDOWN_UNKNOWN`**。
- 三次运行如实记录（含两次测量纪律缺陷 + 一次真实的 `CLOCK_UNCERTAINTY_TOO_HIGH`）；修正后结论稳定。
- `context/current_state.md` 版本表漂移修正（P0001.9.4 / .1 已为 `108ab49` 并 push）。
- 全量测试 **1524 passed / 0 failed / 24 skipped**；`context/status.json.currentProposal` 回到 `null`。

### P0001.9.4.2 — Persistent Equity High-Watermark（已完成）

- 新增 `risk/high_watermark.py`（domain）+ `storage/high_watermark.py`（窄 durable store，`temp → flush → fsync →
  atomic replace`）：`EquityHighWatermarkState` / `ActivationPreconditions` / `HighWatermarkEvidence` / `HighWatermarkTracker`。
- **语义**：drawdown = 自 **trusted activation point** 起相对最高可信权益的回撤（非历史最高权益）；
  activation 必须显式（RECOVERED / flat / 0 挂单 / 无 unresolved / daily PnL known / equity known / 快照新鲜 / equity 一致）。
- **不变量**：peak 单调 + **durable-before-publish**（save 失败不发布）；restart / crash / UTC 午夜都不重置；
  删除状态文件 ≠ 重置（缺失 ⇒ `UNINITIALIZED` ⇒ BLOCKED）；`TRANSFER` ⇒ `INVALIDATED`（要求显式 rebase，不做 cash-flow NAV）。
- `risk/snapshot.py` 支持 `high_watermark=` 注入 ⇒ `peak_equity` / `drawdown` / `drawdown_pct` 来自 durable HWM
  （Paper/Replay 不传即完全不变）；readiness 新增 5 个 reason code：
  `HIGH_WATERMARK_NOT_INITIALIZED` / `HIGH_WATERMARK_INVALID` / `HIGH_WATERMARK_STORE_FAILED` /
  `EXTERNAL_CAPITAL_FLOW_DETECTED` / `EQUITY_MISMATCH`。
- **真实 Testnet 跨进程验收（SC-20）**：Phase A（进程 1）activation 前置条件全满足（local 与 exchange equity
  diff 0.0）并持久化；Phase B（**新进程**）加载同一 activation、peak 未被重置，public market ready、
  2 条业务事件、clock offset 270 / uncertainty 182 ms、income daily known、无新 TRANSFER、recovery RECOVERED、
  最终 flat + 0 挂单 ⇒ **readiness = `live_ready`、scope `testnet_live_ready`、reasons = `[]`**。
- 全量测试 **1584 passed / 0 failed / 24 skipped**；`context/status.json.currentProposal` 回到 `null`。

### P0001.9.5 — Execution Readiness Evidence Binding（已完成）

- 新增 `market/readiness.py`（产品侧 typed `MarketReadinessEvidence` + 纯映射，connector 侧只读事实）；
  `readiness/collector.py`（`ReadinessEvidenceCollector` 从真实 Owner 读取 + `ReadinessProvenance` + digest）；
  `readiness/authority.py`（`ExecutionReadinessAuthority` + `issue_authority` 仅 `LIVE_READY` 可签发 +
  `ExecutionReadinessAuthorityValidator`：TTL / scope / recovery·market·HWM generation / kill switch）。
- 契约升级：`LiveReadinessEvidence.market` 变为 typed evidence（**不再接受裸 `market_ready=True`**）；
  `EnvironmentValidationEvidence` 取代裸 `mainnet_private_validated`（含 id/scope/source/时间，testnet ≠ mainnet）；
  HWM 正式路径只经 `HighWatermarkTracker`；新增「声称 drawdown 已知但 HWM 无确认 peak ⇒ fail closed」一致性校验。
- 两个 epoch：`RecoveryGeneration(discontinuity/invalidation)`、`market_generation`
  （invalidate / resync / 重连推进；唯一入口）。
- **真实 Testnet 授权链验收**：readiness `live_ready`（reasons `[]`、digest `sha256:5f5a…`、market generation 2）
  ⇒ 签发 `testnet_live_ready` 授权（TTL 30 s）⇒ 校验 **valid**；
  真实 private discontinuity ⇒ 同一授权 **invalid（RECOVERY_GENERATION_CHANGED）**；
  market generation 推进 ⇒ **invalid（MARKET_GENERATION_CHANGED）**；请求 Mainnet ⇒ **invalid（SCOPE_MISMATCH）**。
- **D-043 主要条款关闭**（§1.5）；未关闭项：activation/authority 的产品 CLI、主网验证记录自动化。
- 全量测试 **1631 passed / 0 failed / 24 skipped**；`context/status.json.currentProposal` 回到 `null`。

## 进行中能力

无。
0001.4.1 的 live 验证（SC-1 / SC-2 / SC-5）。

## 下一步

- **无授权中的步骤**：P0001.9.3 – P0001.9.5 均已收口，`currentProposal = null`。
  Testnet readiness blocker 集合为空且已有短时执行授权链；下一步（Binance ExecutionAdapter）**必须**由人类
  落盘新提案后才可实施 —— 不得据此推断或启动；实现时仍须逐订单走 `RiskGate`。
  真实下单能力（Binance ExecutionAdapter）**尚未授权**，且 readiness 目前必然 BLOCKED（历史风险未知），
  任何"进入真实交易"的下一步都必须由人类落盘新提案。下一阶段必须由人类/设计方落盘正式 Proposal，
  再由人类下达「读取 … 实施」指令后才可实现（不得从 roadmap / handoff 推断任务）。
- 待人类决定（不阻塞）：① adverse-selection 阈值 X（bps）；② 五分类 bucket 数值分档；
  ③ `RiskLimits` 全部限额数值；④ `MakerPolicyConfig` 生产数值；⑤ 是否补验**主网**私有链路（含 D-036 时钟校正口径）；
  ⑥ 是否轮换测试网 key（`~/.probex/testnet.env`，仓库外 600 权限、未提交）。

## Blocker

无进行中的实现。待人类/设计决定的**业务参数**（不阻塞验收）：
0. Accounting / Risk 的限额数值（`RiskLimits` 全为调用方配置；本阶段只提供机制与默认 fail-closed 行为）；
1. adverse-selection 阈值 X（bps）——实现为必填构造参数，测试值 5.0 仅为测试参数；
2. 五分类 bucket 是否需要数值分档（当前为定性描述，与 `jev-market-v1` 既有语义一致）。
冻结项（已解除，历史记录）：Provider identity 五问已由 P0001.4.1 / P0001.4.2 回答；P0001.5 及其后阶段均已按各自提案实施。
当前冻结项：未获批准的下一阶段不实现；未授权不得新增第三方依赖（含 ccxt / pytest）；产品代码不得出现下单/撤单能力（除独立提案授权）。

## 版本

Git 仓库已初始化；P0001.1 – P0001.9.2.1 均已提交并推送（每个 commit 都能在其检出点独立通过测试）：

| commit | 阶段 | 检出后测试 |
| --- | --- | --- |
| `5d29574` | 协作骨架与提案目录 | — |
| `282ea61` | P0001.1 Market Event + L2 Book + BookHealth | 100 passed |
| `ac3975b` | P0001.2 Event Store + Deterministic Replay | 205 passed |
| `ce2bb41` | P0001.3 MarketState + Feature Engine | 344 passed |
| `c247fff` | P0001.4 Jev Prediction Runtime | 495 passed |
| `2d7d508` | P0001.4.1 OpenRouter transport | 541 passed（4 skipped） |
| `7053a70` … `aeb4ab8` | P0001.6 – P0001.9.2（含 1.9.1 / 1.9.1.1 / 1.9.2.1） | 1190 – 1327 passed |
| `1be451a` | **P0001.9.3** Startup Recovery + Account Reconciliation | 1387 passed（含 P0001.9.3.1 前的基线） |
| `4b69a73` | **P0001.9.3.1** Recovery Contract Closure | 1405 passed（detached worktree 复核） |
| `e5d6921` | **P0001.9.3.2** Discontinuity Notification Reliability | 1416 passed（detached worktree 复核） |
| `108ab49` | **P0001.9.4** Live Readiness Gate（含 D-036 符号修正）+ **P0001.9.4.1** Historical Risk Bootstrap（含审计身份修正） | 1515 passed（detached worktree 复核） |
| `e600e84` | **P0001.9.4.1.1** Full Testnet Readiness Integration Validation | 1524 passed（detached worktree 复核） |
| `2543eea` | **P0001.9.4.2** Persistent Equity High-Watermark | 1584 passed（detached worktree 复核） |

`CLAUDE.md` 与 `.gitignore` 被使用者全局 gitignore（`~/.gitignore_global`）排除，未纳入版本控制。
P0001.9.3（`1be451a`）与 P0001.9.3.1（`4b69a73`）均已提交并 **push 到 `origin/master`**；
P0001.9.4 与 P0001.9.4.1 已作为 commit `108ab49`（1515 passed）、
P0001.9.4.1.1 已作为 `e600e84`（1524 passed）、P0001.9.4.2 已作为 `2543eea`（1584 passed）
依次提交并 **push 到 `origin/master`**；当前工作树状态以 `git status` 为准。
