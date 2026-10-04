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

`context/status.json` 的 `currentProposal` 为 **`"P0001.16"`**（**实现中 / 未 CLOSED**）：人类 2026-10-04
「选择 B，并批准 P0001.16 进入实现」；SC-11/SC-12/SC-13（真实 TESTNET 挂单/成交/flat）尚未达成，因此不得标记完成。

> 历史说明（superseded）：本文件早期段落中的 `"P0001.9.7"` / 各 P0001.9.x 状态描述只反映当时事实；
> 判断当前 Proposal 一律以 `context/status.json` 为准。

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

### P0001.9.6 — Binance USDⓈ-M ExecutionAdapter（已完成）

- 新增 `connectors/binance/execution/`（**写边界**，与只读 private 层分离）：`rest.py`（POST/DELETE/GET `/fapi/v1/order`，
  结构化错误：4xx 业务码 ⇒ `ExecutionRequestRejected`、5xx/429/408/不可解析 ⇒ `ExecutionOutcomeUnknown`）、
  `parsing.py`（严格解析 + `SubmitClassification`）、`adapter.py`（`BinanceExecutionAdapter`）。
- **第一版范围固定**：LIMIT + GTX(post-only) + `positionSide=BOTH`；`post_only=False` / 未知类型 / 步长价格不合法 **本地拒绝**，
  **不自动 round**；`clientOrderId` 原样作为 `newClientOrderId`（幂等主键）。
- **submit 三分类**：可解析 ACK ⇒ `OrderAccepted`；业务拒绝 ⇒ `OrderRejected`；其余 ⇒ **UNKNOWN ⇒ 空事件、绝不重试**，
  只允许 `query_order(origClientOrderId)` 收敛；cancel 未确认绝不伪造 CANCELED。
- **authority 紧邻写请求**（P0001.9.5）：TTL/scope/generation/HWM/kill switch 任一失效 ⇒ 本地拒单且**零 HTTP**；
  **cancel 不要求 LIVE_READY**（降险不可被锁死）。RiskGate 仍逐单执行（未改 `execution/**`）。
- **user stream 桥接**：`bridge_user_event()` 把真实 `ORDER_TRADE_UPDATE` 转成既有 `ExecutionEvent`；成交只来自 trade execution
  （query 不造 Fill）；与本地终态冲突的转换跳过并计数。
- **真实 Testnet 验收 PASS**：readiness `live_ready` ⇒ 签发 authority ⇒ 真实 submit（`NEW`，orderId 28607085026）→ cancel（`CANCELED`）；
  响应丢失注入 ⇒ UNKNOWN 且 **POST 只发 1 次**、query 收敛、**无重复单**；撤单响应丢失 ⇒ 空事件后 query 收敛；
  最终 `position 0` / `open orders 0`；全程 8 条真实 `ORDER_TRADE_UPDATE` 入既有 Tracker。
- **implementation correction（2026-09-28，人类裁决）**：`open_orders()` / `recent_fills()` 在事实 provider
  未注入/读取失败时**抛 `ExternalFactsUnavailableError`**，绝不返回空集合（**UNKNOWN ≠ EMPTY**）；
  provider 契约 = `ExternalFactsProvider` Protocol。
- 全量测试 **1693 passed / 0 failed / 24 skipped**；`context/status.json.currentProposal` 回到 `null`。

### P0001.9.7 — Live Execution Orchestration（实现中；Phase B 阻塞）

- 新增 `live/`：`orchestrator.py`（`LiveExecutionOrchestrator`：单轮固定顺序、QuoteAction 映射、同侧单报价不变量、
  authority 门、reconciliation 触发、stop 语义）、`telemetry.py`（每轮事实 + 累计计数）。
- 新增 `connectors/binance/execution/external_facts.py`：`PrivateExternalFactsProvider`（`ExternalFactsProvider` 的产品实现，
  读取失败 ⇒ 抛错；真空 ⇒ `()`）。
- `position` 改为**派生事实**（来自 accounting；显式传入必须一致，否则 fail closed）；`existing_orders` 只来自 Tracker。
- **Phase A（observe-only）真实 Testnet 10 分钟 / 145 轮 PASS**：readiness `live_ready`、authority 已签发、
  **全部动作计数为 0（零写请求）**、无未捕获异常、stop 后 0 挂单 / position 0。
- **Phase B（write-enabled）BLOCKED**：① 账户杠杆 **20 ≠ 提案要求的 1**（产品不得修改杠杆 ⇒ 需人类在交易所设置）；
  ② 无 prediction 源（无 `OPENROUTER_API_KEY`）⇒ P0001.7.1 正确地禁止新增暴露 ⇒ 不可能产生 PLACE。
- 测试：unit 1083 / integration 268 / fault 308 / replay 49 passed；全量 **1732 passed / 0 failed / 24 skipped**。
- `currentProposal` 保持 `"P0001.9.7"`（**未关闭**）。

## 进行中能力

- **P0001.9.7 Phase B**：**前置条件已解除**（杠杆经仓库外 harness 改为 1x 并只读复验；
  `OPENROUTER_API_KEY` 从仓库外 `~/.probex/openrouter.env` 注入且真实 SystemOne 调用通过）。
  两次真实 Testnet 运行（32 分钟窗口 + 预热）**仍未产生任何报价** ⇒ **SC-24 – SC-28 未达成、根因未定位**（不猜）；
  下一步：harness 增加逐轮 telemetry 落盘后短诊断（产品代码不动）。安全检查：open orders 0 / position 0 / leverage 1。
0001.4.1 的 live 验证（SC-1 / SC-2 / SC-5）。

## 下一步

- **无授权中的步骤**：P0001.9.3 – P0001.9.6 均已收口，`currentProposal = null`。
  产品现已具备 **Testnet 写执行能力**（LIMIT+GTX），但**主网写路径未验证**、也未获授权；
  任何真实交易/策略 live loop 都必须由人类落盘新提案后才可实施 —— 不得据此推断或启动。
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
| `1fa7dcc` | docs: 版本表补记 P0001.9.4.1.1 / P0001.9.4.2 | 1584 passed |
| `a2ab8da` | **P0001.9.5** Execution Readiness Evidence Binding | 1631 passed（detached worktree 复核） |
| `afc0196` | **P0001.9.6** Binance USDⓈ-M ExecutionAdapter（含 UNKNOWN≠EMPTY correction） | 1693 passed（detached worktree 复核） |
| `d4939a4` | **图表 / UI 产品化**（Chart Workbench：K 线 / 指标 / 画线 / semantic overlays / ECharts 面板） | 2303 passed（detached worktree 复核） |
| `7987cd5` | **P0001.14** Runtime Decision Loop Integration | 2347 passed（detached worktree 复核） |
| `14ac3cb` | **P0001.15** Instrument Domain + Venue Integration Contract | 2401 passed（detached worktree 复核） |

`CLAUDE.md` 与 `.gitignore` 被使用者全局 gitignore（`~/.gitignore_global`）排除，未纳入版本控制。
P0001.9.3（`1be451a`）与 P0001.9.3.1（`4b69a73`）均已提交并 **push 到 `origin/master`**；
P0001.9.4 与 P0001.9.4.1 已作为 commit `108ab49`（1515 passed）、
P0001.9.4.1.1 已作为 `e600e84`（1524 passed）、P0001.9.4.2 已作为 `2543eea`（1584 passed）、
P0001.9.5 已作为 `a2ab8da`（1631 passed）、P0001.9.6 已作为 `afc0196`（1693 passed）
依次提交并 **push 到 `origin/master`**；
当前工作树状态以 `git status` 为准。

> **流程要求（防漂移）**：
> ① 本表只登记**阶段 commit**（即带 Proposal 的实现提交）；纯 docs 提交由 `git log` 体现，**不入表**
> （否则会出现"记录自己 hash"的自引用循环）；
> ② 每次收尾 commit 之后必须**立即**补行（hash + 该 commit 的 detached worktree 结果），
> 并在提交前自检「本表是否已包含即将产生的阶段 commit hash」；
> ③ 判断"当前是否已提交"以 `git log` / `git status` 为准，本表只是索引。

## P0001.9.7.1 Private Latency Cold-Start Bootstrap（实现中）

- 人类裁决（本会话）：不用探测单旁路、不普遍放宽 `PRIVATE_LATENCY_UNKNOWN`；把"从未有可测事件"建模为 `UNOBSERVED`。
- 切片 1 已落地：`UNOBSERVED` / `BOOTSTRAP_ELIGIBLE` / reason `PRIVATE_LATENCY_UNOBSERVED` / 证据计数 / gate 分类（`readiness/{types,evidence,gate,__init__}.py`）。
- 安全性：`issue_authority` 仍要求 `LIVE_READY` ⇒ 尚无任何路径能签发 authority；fail closed 未放宽。
- 未落地：BOOTSTRAP authority 签发/校验、首笔事件后的升级闭环、orchestrator 强制、真实 Testnet 冷启动验证。
- 测试：全量 1735 passed / 0 failed / 24 skipped。

### P0001.9.7.1 切片 2 已落地 / 切片 3 待做

- 已落地：`PrivateLatencyStatus.UNOBSERVED` + `BOOTSTRAP_ELIGIBLE` + reason `PRIVATE_LATENCY_UNOBSERVED`（gate 分类）；
  `readiness/bootstrap.py`（`BootstrapEligibility` / `BootstrapAuthority` / `issue_bootstrap_authority` / `BootstrapAuthorityCoordinator`）；
  validator 的 `validate_bootstrap` + `BOOTSTRAP_SUPERSEDED` 等正式原因；NORMAL authority 拒绝非 HEALTHY latency。
- 待做（切片 3）：写路径（adapter / authority context / orchestrator）识别并强制 BOOTSTRAP（post-only、notional cap、
  按 write attempt 计数的 `max_orders=1`、REPLACE 不得产生第二笔写入），随后真实 Testnet 冷启动闭环验证（SC-1…SC-7）。
- 测试：全量 1748 passed / 0 failed / 24 skipped；未 commit。

### P0001.9.7.1 切片 3（写边界接入）已落地；Testnet 闭环验证待重跑

- 已落地：`BootstrapWriteGate`（写边界校验 + 额度在网络调用前消耗）、adapter 按 authority kind 分派、20 条写路径单测。
- 待办：orchestrator 的 BOOTSTRAP lifecycle 识别；（因本机网络层故障）真实 Testnet 冷启动 A/B/C verdict 尚未取得。
- 测试：全量 1768 passed / 0 failed / 24 skipped；未 commit。

### P0001.9.7.1 切片 2/3 全部落地；Testnet 冷启动 verdict 仍待取得

- 已落地：`UNOBSERVED` 语义、BOOTSTRAP authority 签发/校验/闭环 coordinator、写边界 `BootstrapWriteGate`
  （`check` 只读 / `authorize` 消耗）、adapter kind 分派、orchestrator kind 分派。
- 待办：harness 侧修复 private-link 生命周期（listenKey ACTIVE + continuity）后重跑真实 Testnet cold-start（§六），
  以取得 A/B/C verdict。
- 测试：全量 1772 passed / 0 failed / 24 skipped；未 commit。

### P0001.9.7.1 / P0001.9.7.2 收口

- P0001.9.7.1：**已完成（代码完成 / live happy-path deferred）**，真实 Testnet verdict = C（fail closed，第二笔写零网络调用）。
- P0001.9.7.2：**已完成**（price/quantity Decimal normalization ownership = 执行边界显式配置的归一化步骤）。
- 短 Testnet execution smoke：**PASS**（一笔 post-only 被接受 → cancel → 0 挂单 / 0 持仓）。
- 全量测试 1790 passed / 0 failed / 24 skipped；`currentProposal = null`；未 push；未跑 30 分钟策略表现测试。

### P0001.9.7 收口（2026-09-29）

- P0001.9.7 = **Completed for development scope**；原 **≥30 min Phase B deferred / 从开发验收移除**（提案 §1.8）。
- 替代开发验收：real Testnet `CONFIRMED_ACCEPTED` + Decimal tick normalization + cancel lifecycle +
  zero orphan + zero residual open orders + zero position + Mainnet write 0 + 1790 tests pass。
- 仍 deferred（不宣称完成）：BOOTSTRAP live happy path（accepted → latency → supersede → NORMAL）；cold-start verdict = C。

### P0001.10 Product API / UI / Reports（已完成）

- `product/`：`SystemSnapshot` 读模型（`Fact` 显式 known/unknown；REPLAY/PAPER/TESTNET/LIVE 共用 schema）。
- `api/`：stdlib 只读 REST（`/api/v1/status|market|prediction|strategy|risk|orders|portfolio|readiness|evidence|snapshot|schema`），非 GET 一律 405。
- `ui/index.html`：engineering console；`reports/`：`RunSummary`（只汇总事实）。
- 测试：全量 **1819 passed / 0 failed / 24 skipped**（+29）。未 commit（未授权）。

### P0001.10.2 / P0001.10.3（已完成）

- .10.2：`reports/`（RunSummary：JSON + Markdown，确定性）+ `ui/`（工程控制台，10 页面，纯静态，只经 Product API）；
  API 增加只读 `/api/v1/reports/run-summary` 与 `/ui/*` 静态服务。
- .10.3：`cli/`（只读、机器优先、稳定退出码 0/2/10/11/20/30、stdout/stderr 分离、无写命令）。
- 测试：全量 **1844 passed / 0 failed / 24 skipped**（+25）。未 commit（未授权）。

### P0001.11 Product Operations Foundation（已完成）

- Config Provenance（`product/provenance.py`）：来源优先级 CLI>ENV>FILE>CONSTRUCTOR，只选择不造默认；secret 只存引用名。
- Run Registry（`storage/run_registry.py` + `reports/lifecycle.py`）：append-only + 原子 record；崩溃 ⇒ `INCOMPLETE`；单 writer；无 retention。
- Metric Contract（`reports/metrics.py`）：10 指标含公式/时间基准/Owner/UNKNOWN 条件/采样；`run_mdd` ≠ 风控 drawdown；PF 无亏损 ⇒ UNKNOWN。
- Unified Blockers（`product/blockers.py`）：owner/reason_code/severity/message/source_ref，去重键 `owner+reason_code+source_ref`，不排序不丢弃。
- Capability Manifest（`api/capabilities.py`）：由路由表 + CLI 注册表生成，`api.write = unavailable_by_design`（测试固定）。
- 产品表面：`schema_version = "2"`；新端点 capabilities/metrics/blockers/runs(+show/compare)；CLI runs/run show/run compare/metrics/capabilities/blockers；UI 新增 metrics/capabilities 页。
- 测试：全量 **1937 passed / 0 failed / 24 skipped**。commit `080bfab` 已 push（P0001.10 阶段）；本阶段改动**未 commit**。

### P0001.11.1 Runtime Run-Lifecycle Integration（已完成）

- `runtime/session.py`：`RuntimeSession`（establish identity → create_run → running → graceful stop → finalize COMPLETED；
  异常 ⇒ INCOMPLETE + 重抛）；`SessionSummaryFacts` 只携带已记录事实；config fingerprint 建立时绑定、漂移即拒绝；
  四模式共用一条路径；本层不含任何交易能力（不 import strategy/risk/execution/connectors，测试固定）。
- 测试：全量 **1955 passed / 0 failed / 24 skipped**（+18）。真实 replay run 经 API/CLI 可直接看到（COMPLETED / INCOMPLETE）。
- 未接线的部分：现有 runner 与 `live/orchestrator.py` 尚未调用 `RuntimeSession`（后续阶段显式接线）。

### P0001.11.2 RuntimeSession Production Wiring（已完成）

- `runtime/wiring.py`：`SessionHost`（start / run_feed / finish / terminate / ctx-manager）+ 四个模式入口；
  `finish()` 先调用原始 Owner 的 stop（live = `orchestrator.stop`），成功后才 finalize `COMPLETED`；
  stop 失败 ⇒ `INCOMPLETE` + 重抛；runtime 层 duck-typed、无交易 import、无 daemon/线程。
- 真实证据：REPLAY / PAPER 自动登记 `COMPLETED`（真实 ReplaySource / PaperBroker）；TESTNET stop 失败 ⇒ `INCOMPLETE`；
  LIVE wiring 以 `orchestrator.stop` 为原 Owner，结束状态 `STOPPED`；四者经 `/api/v1/runs` 可见。
- 测试：全量 **1973 passed / 0 failed / 24 skipped**（+18）。提交 `2085d2d` / `c89e516` 已 push；本阶段改动未 commit。

### P0001.12 Market Visual Workbench（已完成）

- `product/market_timeline.py`（时间线：bucket/window/max_points 有界；特征只搬运 Owner 事实）
  + `product/market_projection.py`（depth 热图 / trades / health / overlays / BoundedMarketHistory / ReplayControl）。
- API：`GET /api/v1/market/{timeline,depth,trades,health,overlays}`、`GET /api/v1/runs/<id>/market`、
  `POST /api/v1/replay/*`（local replay control，仅 REPLAY；与交易写路径隔离）；capabilities 新增 `api.simulation_control`。
- UI：Market 页 = 工作台（Canvas 热图 + 特征面板 + 健康时间条 + overlays + replay 控件；零第三方依赖）。
- 测试：全量 **2017 passed / 0 failed / 24 skipped**（+44）。提交 `055df59`（P0001.11.2）已 push；本阶段未 commit。

### P0001.12.1 Product Surface Architecture（已完成：信息架构 + gap audit）

- 一级导航固定 5 个 Surface：Monitor / Market / Activity / Performance / System（`ui/app/surfaces.js` 为单一来源）；
  全局 Header（Mode/Environment/Venue/Symbol/Runtime/Health/Data Time）+ 全局 Blocker Strip。
- Activity 合并 Prediction/Strategy/Orders/Evidence 为因果链；Performance 以 Run 为单位；System 承载 Health/Risk/Readiness/
  Execution(P0001.13 插槽)/Configuration/Capabilities；Market 增加 Live/Replay/Run Review。
- **Gap audit**：12 类需求可移动/组合满足；需新 Product API 的 5 项（G1 fill 证据、G2 raw facts、G3 equity 序列、
  G4 prediction/accounting 健康、G5 authority kind/TTL/generation）；Execution Safety 明细属 P0001.13。
- 未做大规模视觉重构（遵守提案指示）；测试全量 **2030 passed / 0 failed / 24 skipped**（+13）。未 commit。

### P0001.12.2 Surface Capability Completion（已完成，严格 G1–G5）

- G1 fill 级证据（`recent_fills` + trace `fill` 阶段）；G2 `GET /api/v1/facts/<kind>/<identity>`（有界/截断/404/400）；
  G3 `GET /api/v1/portfolio/timeline`（有界 equity/exposure，未接线 503）；G4 `health.prediction_provider`/`accounting`；
  G5 readiness 的 authority kind/TTL/generation。`schema_version = "3"`。
- UI：Activity（fill + raw facts）、Performance（equity/exposure 时间线）、System/Health（G4）、System/Readiness（G5）。
- 测试：全量 **2054 passed / 0 failed / 24 skipped**（+24）；`currentProposal = null`；未 commit。
- 未做：P0001.12.3（Assistant / Action Gateway）、P0001.13（rate limit / latency / reconciliation 后端）。

### P0001.12.3 AI Assistant & Action Gateway（已完成）

- Action Plane：`actions/`（Manifest 23 条：L0 READ 6 / L1 PRODUCT 5 / L2 RUNTIME 4 / L3 CAPITAL 8 全 `unavailable_by_design`；
  Gateway（拒绝 CAPITAL handler、Confirmation、审计、UNKNOWN 保留）；Confirmation（action+参数指纹+runtime+TTL、一次性）；Audit（有界+参数指纹））。
- Assistant：`assistant/`（上下文 + 建议动作仅来自 Manifest 且按 mode 过滤 + 确定性 explain，不调用 LLM）。
- 表面：`GET/POST /api/v1/actions*`、`GET /api/v1/assistant/context`；CLI `actions` / `action describe|invoke`；
  UI 全局可折叠 Assistant Drawer（按钮来自 Manifest，确认流程绑定 confirmation_id）。
- 测试：全量 **2101 passed / 0 failed / 24 skipped**（+47）；`currentProposal = null`；未 commit。
- 未做：P0001.13（rate limit / latency / reconciliation 后端）；未开放任何 CAPITAL 能力。

### P0001.13 Execution Safety & Operations Surface（已完成）

- `execution_safety/`：policy（阈值全必填无默认）/ venue facts（复用 TradingRules；rate/order 待真实证据 ⇒ UNKNOWN）/
  governor（无 retry；EXHAUSTED 只挡新增暴露；降险永不被阻断）/ latency（五阶段，样本不足 ⇒ UNKNOWN 非 0ms）/
  health（四态、policy 驱动、required UNKNOWN 不得 HEALTHY）/ projection（只读 + blockers + 受控 reconciliation 入口）。
- 接线：BlockerOwner +EXECUTION/VENUE；snapshot.execution_safety；6 个只读端点；5 个 `execution.*` READ 动作；
  `runtime.request_reconciliation` → AVAILABLE L2（需确认）；AssistantContext 6 字段；CLI `execution ...`；UI 三处落点。
- 测试：全量 **2134 passed / 0 failed / 24 skipped**（+33）。未 commit。

### Closure Slice 1 — Product Assembly（已完成并复验）

- `runtime/assembly.py` 是唯一 in-repo 装配入口（默认 REPLAY / 127.0.0.1；TESTNET/LIVE 需显式）；组装 ConfigSnapshot、RunRegistry、
  RuntimeSession/SessionHost、ProductService、AssistantService、ActionGateway（10 个基线只读/产品态 handler）、API/UI server。
- `runtime/state.py`：RuntimeState（STARTING/RUNNING/STOPPING/STOPPED/FAILED）+ 唯一 Owner；`quoting` 仅在已知时给出。
- 投影：Snapshot.health 增加 runtime_state/detail/quoting/run_id/since_ms；Monitor 与 System/Health 可见。
- 真实证据：仓库命令启动 ⇒ snapshot 真 identity + RUNNING + quoting=false；UI 可开；actions 可用；inspect.snapshot SUCCEEDED；capital 409；
  SIGTERM ⇒ index `start`+`finalize`、durable run COMPLETED、shutdown state=STOPPED。
- 修复 2 缺陷：env 目录未生效、signal 未 finalize（同线程 shutdown 死锁）。
- 测试：全量 **2150 passed / 0 failed / 24 skipped**；F-01 CLOSED、F-11 PARTIAL（quoting/loop 真实数据属 Slice 2）。

### Closure Slice 2 — Real Run Lifecycle（已完成并复验）

- F-10 CLOSED：active marker（`active.json`，原子、同持久化域、pid 探活、无 TTL 真相）；运行中 `RUNNING`、异常/无 marker `INCOMPLETE`、finalize `COMPLETED`。
- F-02 CLOSED：`runtime/provider.py`（复用 ReplaySource/MarketBook/FeatureEngine/BoundedMarketHistory/PaperBroker/OrderManager）；
  `runtime/assembly.py` 仅为 composition root；REPLAY 与 PAPER 真实跑通，`/runs` 运行中 `RUNNING`、timeline 有真实事实、SIGTERM ⇒ `COMPLETED`。
- CLI：`--event-store` 必填（REPLAY/PAPER），bounds 来自 `--config-file`。
- 测试：全量 **2165 passed / 0 failed / 24 skipped**。

### Closure Slice 3 — Real Facts + Assistant Wiring（已完成并复验）

- `runtime/safety_config.py`：policy / rules / limit definition / usage 的构造（缺键 ⇒ 不构造，保持 UNKNOWN）。
- `runtime/accounting_facts.py`：AccountingCore → 稳定只读事实（显式取值、只收标量、异常 ⇒ UNKNOWN 不外抛、`health()`）；
  assembly 仅注入 provider（不再拼字段）；仅在显式 `accounting.initial_balance` 时构造。
- `runtime/provider.py`：每轮 market state 后转发 account 事实到 `BoundedAccountTimeline`（失败隔离、不伪造 0）。
- `/api/v1/assistant/explain`（确定性 explain，无 LLM）；`profiles/trial-local.json`（trial-only、非默认）。
- 真实 E2E：REPLAY + PAPER 均通过（market 11 states / portfolio equity 10000 / limits 真实 / health DEGRADED 带 reasons /
  reconciliation_duration samples=1 / explain 200 / SIGTERM ⇒ COMPLETED）。
- 测试：全量 **2186 passed / 0 failed / 24 skipped**。

### Closure Slice 3 收口 — F-03/F-05 真实边界证据 + flaky 修复（已完成，CLOSED）

- **F-03 CLOSED（venue usage 真实）**：`execution_safety/venue_usage.py`（`VenueUsageCollector` / `HeaderCapturingFetcher`）
  解析 `X-MBX-USED-WEIGHT*` / `X-MBX-ORDER-COUNT*`；三个真实 fetcher 捕获 `last_headers`；
  **移除** config 手填用量路径。真实证据：public `/fapi/v1/time` 含 `x-mbx-used-weight-1m`；
  signed 只读 `/fapi/v2/account` 解析出 `used_weight=532`（source `X-MBX-USED-WEIGHT-1M`）；
  `used_orders=None`（该头未返回 ⇒ UNKNOWN，不伪造 0）。
- **F-05 CLOSED（五阶段真实边界）**：`ExecutionEngine.latency_observer` + `LiveExecutionOrchestrator.latency_observer`
  + `runtime/latency_observer.py::ExecutionLatencyObserver`（D-036 `raw+offset`、负值只计数不 clamp）。
  PAPER 自然回路（经既有 RiskGate → PaperBroker）产生 `decision_to_submit=1` / `submit_to_ack=1` / `cancel_to_ack=1`；
  `reconciliation_duration≥1`；`event_receive_lag` 边界已接但 TESTNET 只读 private smoke 未产生事件 ⇒ UNKNOWN（环境原因，已留证据）。
- **F-04 / F-06 / F-07 CLOSED**（policy 注入 / explain 端点 / 基线 handler）。
- **flaky 定位并修复（测试竞态，非产品缺陷）**：`test_account_provider_failure_does_not_break_the_feed`
  在 `runtime.start()`（已起 feed 线程）之后才注入 hostile provider，11 条事件可能在赋值前被消费完 ⇒ 实测 2/20 失败。
  修复：把注入点前移到 `MarketFeedProvider.start` 之前（测试内 `mock.patch.object`）；修复后单测 0/40 失败。
- **detached 全量复核**：修复后**连续 3 次** `Ran 2186 tests ... OK (skipped=24)`（0 failed）。
- `context/status.json.currentProposal = null`；closures Slice 1–3 全部收口；**不进入 Slice 4 之前的最后一个门槛已满足**。
- 测试：全量 **2186 passed / 0 failed / 24 skipped**（detached worktree 复核 ×3）。

### Closure Slice 4 — 真实 Product 链路（F-08/F-09/F-13/F-16/F-19，已完成）

人类授权（本会话）：closure 继续，关闭 F-08/F-09/F-13/F-16/F-19，**不新建 Proposal 编号**；
原则 = 把已有事实/配置/页面真正连成产品链路（**不新增交易能力**）。

- **F-08 CLOSED（Activity 完整因果链）**：`evidence.trace` 成为按时间排序的统一 trace
  （`market_state → prediction → maker_decision → risk → readiness → normalization → order(submit) → ack →
  execution_event → cancel → fill → unknown → reconciliation`）。
  `ExecutionEngine.decision_log`（allow+reject，引用既有 RiskDecision）；`execution/normalization.py` 新增
  `NormalizationEvidence` + `BoundedNormalizationEvidenceLog` + `normalize_with_evidence()`（执行边界记录
  输入/归一值/舍入/reject reason，`OrderNormalizer` 逻辑未改）；ack latency 只读 Slice 3 真实 observer；
  受控 reconciliation 落 `_TraceEvent`。每条 `TraceEntry` 带 `ts` / `identity_kind` / `latency_ms`；
  缺阶段显式 `absent` + reason（`fill` 例外：`recent_fill_limit=0` = 未暴露成交）。`schema_version` → **4**。
- **F-09 CLOSED（Reason 解释层）**：`product/reason_catalog.py`（72 条，presentation-only，不参与决策）；
  原始 code 永远保留，未收录 ⇒ `暂无解释`（不猜）。复用：`/api/v1/reasons`(+`/<code>`)、snapshot blocker `explanation`、
  Assistant `blocker_explanations`、CLI `reasons`/`blockers`/`explain`、UI `reasonCell`。
- **F-13 CLOSED（Config Resolver 真接线）**：`env_config_entries()`（`PROBEX_CONFIG_*` + `PROBEX_SECRET_*` 只存引用名）；
  `build_profile_from_args` 收集 ENV+FILE+CLI，用同一 `resolve_config`（CLI > ENV > FILE > CONSTRUCTOR）；
  assembly 解析一次（`_resolved_config`），System→Configuration 显示值/来源/fingerprint/secret 引用；fail-closed 未变。
- **F-16 CLOSED（Navigation + Legacy）**：`product/navigation.py` 与 `ui/app/navigation.js` 单一契约（测试固定一致）；
  Assistant `navigate.surface`/`select.entity` 与 UI 共用；Run→Run Review、Run→Activity、event→Market ts、
  Blocker→System section、Order/Fill→Evidence/Raw Facts。`risk`/`readiness`/`capabilities` 旧页面已删除（被 System 覆盖），
  其余降级为可到达 detail route ⇒ 无死页面；顺带修复 `system/page.js` 语法错误（整个 System Surface 曾无法加载）。
- **F-19 CLOSED（Runs 有界化）**：`JsonRunRegistry.page(limit, offset)`（`MAX=200` / `DEFAULT=50`）+ `GET /api/v1/runs`
  的 `limit/offset` 与 `pagination` 元数据；UI Performance 分页读取；CLI `runs --limit --offset`；compare/show 不受影响。
- **产品级 E2E（真实启动，非 harness）**：A 因果 trace（含 risk/normalization/ack/cancel/reconciliation）；
  B reason UX（catalog + 未知 code + blocker explanation）；C config provenance（CLI>ENV>FILE，secret 仅引用）；
  D navigation targets；E runs 分页无重复/遗漏。真实 `python3 -m runtime.assembly` 启动 + HTTP 证据齐全；
  SIGTERM ⇒ durable `COMPLETED` + active 清空。
- 测试：全量 **2232 passed / 0 failed / 24 skipped**（+46 新测试）。
- `context/status.json.currentProposal = null`。

### Closure Slice 5 — Operational Posture（F-12/F-15，已完成）

人类授权（本会话）：closure 继续，关闭 F-12 / F-15，并补齐 network exposure/auth、structured logging、
retention/growth policy、process liveness、crash/restart 最小闭环（**不新增交易能力**）。

- **遗留清理**：上一轮遗留的 `runtime.assembly --port 8793`（PID 47249，修复前旧代码，shutdown 卡死、HTTP 无响应）
  经 SIGTERM/SIGINT 无效后 SIGKILL；端口释放；durable run `replay-1790694780015` 保持 **COMPLETED**（未删除/改写）；
  无 active marker 指向它。
- **F-12 CLOSED（Network/Auth）**：默认 loopback 不强制认证；非 loopback 必须 `--allow-non-loopback` + token 的
  secret 引用，缺任一拒绝启动；token 值不进 snapshot/provenance/logs/audit/异常（provenance 只留 `env:NAME`）；
  所有 `/api/v1/*` 需 `Authorization: Bearer`（含 actions/execution，无匿名旁路）；`/health/live` 免认证。
- **F-15 CLOSED（Logging）**：`runtime/observability.py`（stdlib logging，单行 JSON：ts/level/event/component/
  runtime_id/run_id/mode/reason_code）；secret/signature/bearer 一律 redact；文件 sink 支持显式轮转；
  覆盖 startup/shutdown/run/action/api/auth/reconciliation/retention/feed failure 等事件。
- **F-15 CLOSED（Retention）**：`storage/retention.py` 显式 policy（默认 **UNBOUNDED**，不隐式删除）；
  只删有 finalize 的旧 run（record + 原子索引压缩），active run 永不删；event store 永不自动删除；
  `run_retention()` 记录 `retention_prune`；System→Ops→Retention 暴露 bounded/unbounded/policy/last prune。
- **Health split**：`runtime/ops.py` 四层互不替代（PROCESS_LIVE / RUNTIME_RUNNING / TRADE_READINESS /
  EXECUTION_HEALTH）；`GET /health/live`（免认证）+ `GET /api/v1/ops` + `snapshot.ops`。
- **Crash/restart**：graceful ⇒ COMPLETED + marker 清除；SIGKILL ⇒ INCOMPLETE（不伪造 COMPLETED）；
  `active_run_id` 增加 PID 复用（启动时刻）与僵尸进程防护（仍未使用 TTL）；restart 后 run history / HWM / config 仍在。
- **表面**：System→Ops section、Monitor 四层摘要、CLI `ops status|retention|logging|network`、
  Assistant `explain.entity(kind=ops)`（复用 F-09 catalog，无新写 action）；顺带修复 Monitor 丢失 `SURFACES` import
  （Slice 4 引入），新增 UI 模块静态守卫测试。
- 测试：全量 **2271 passed / 0 failed / 24 skipped**（+38 新测试）。
- `context/status.json.currentProposal = null`。

### Read-model / UI wiring polish（F1/F2/F3/F4/F5/F7 全部 CLOSED；F6 不动）

来源：一次真实产品验收运行暴露的读模型/UI wiring 缺陷（closure 修复，**不新建 Proposal**）。边界：无新策略、
无 F6 prediction→strategy 回路、无新 metric 算法、无第二套 raw fact / accounting owner、无 UI 大改版。

- **F1 CLOSED（Monitor position 映射）**：`AccountingFactsProvider` 正式暴露 `position_known` + `position_qty`
  （qty 标量含 0.0 ⇒ known；取不到 ⇒ 两者 UNKNOWN，**不伪造 0**）；`ProductService._portfolio` 只消费 typed facts
  （删掉 `is True` 的 Fact 内部结构猜测）。Monitor 用 `positionFact()` 区分 `flat (0)` / known qty / UNKNOWN。
- **F2 CLOSED（Market health 字段映射）**：`_market` 从真实 `DataQuality.book_health`（`BookHealth` 枚举）映射，
  `healthy` **只**由 `book_health == healthy` 推导；`tradeable` 独立；新增 `book_health` / `book_age_ms`；
  `HealthView.market_healthy` 与 Monitor **同一 projection**（不再各自判断，也不从 tradeable 反推）。
- **F3 CLOSED（Ops `[object Object]`）**：`render.js` 新增通用 `valueView`（Fact 解包 / 标量表格 / 嵌套 `<details>`）
  与 `jsonDetail`；`factRows` 改用 `valueView`；`System → Ops` 的 network/auth/logging/retention 与
  `System → Configuration` 的 entries 都改为可读摘要 + raw JSON detail。全页面扫描证明无 `[object Object]`。
- **F4 CLOSED（RunSummary provider 未接线）**：assembly 新增 `_build_summary_facts()`（从既有 `AccountingCore` /
  `BoundedAccountTimeline` / `OrderTracker` / `ExecutionEngine.rejections` 汇总，**不重算 metric**）与
  `_durable_run_summary(run_id)`（既有 `RunRegistry` + `build_run_summary`）；`RunSummary` 端点支持 `?run=<id>`
  （known ⇒ 200、unknown ⇒ 404、缺 facts ⇒ UNKNOWN 字段而非 503）。真实证据：REPLAY `realized_pnl=0 / fees=0 /
  run_mdd=0 / final_position=0`；PAPER `order_counts={FILLED:1} / fills=1 / final_position=0.001`；
  Compare 对两个 COMPLETED run 产出 known `fees/realized_pnl/run_mdd`（其余诚实 UNKNOWN）。
- **F5 CLOSED（Raw Facts provider 未接线）**：assembly `_raw_fact_lookup` 接既有 Owner（order⇒OrderTracker、
  fill⇒FillLedger 视图、decision⇒maker_decision、execution_event⇒订单生命周期事实、prediction⇒record provider），
  canonical identity 直接查找；`RawFactProviderUnavailable` 区分「provider 未接线 ⇒ 503」与「事实不存在 ⇒ 404」；
  不新建第二套事实存储、payload 无 secret/signature。真实证据：PAPER 真实 order+fill ⇒ `facts/order/<cid>` 与
  `facts/fill/<cid>` 均 200（FILLED / trade_id 真实），missing ⇒ 404。
- **F7 CLOSED（Activity 缺阶段文案）**：`reasonCell(null)` 不再输出 `UNKNOWN`；Activity `stageReason()` 把
  `outcome=absent` 渲染为 **ABSENT + 「本次运行未产生该阶段事实」**（`unavailable` 语义单独标注），
  原始 reason 仍以 muted raw 保留；`UNKNOWN UNKNOWN` 不再出现。
- 守卫测试：`tests/unit/test_read_model_polish.py`（12）+ `tests/integration/test_read_model_polish_e2e.py`（3，含
  全页面 `[object Object]` 扫描）+ `tests/ui/render_page.mjs`；同步修正 3 个编码旧语义的旧测试 + 1 个 facts 未接线语义。
- 测试：全量 **2286 passed / 0 failed / 24 skipped**。
- **遗留（本轮未改，F6 范畴）**：`MarketFeedProvider` 自建了一个**未被使用**的 `PaperBroker`，真实执行边界用的是
  `ExecutionEngine.manager.adapter` 上的另一个实例；两个实例并存是架构噪音（不影响本次修复的读模型）。

### 图表 / UI 产品化（Chart Workbench，已实施并真实浏览器验收）

人类批准范围；技术选型固定 `klinecharts@10.0.3` + `@klinecharts/extension@0.1.0` + `echarts@6.1.0`
（**不使用** `@klinecharts/pro`）。记录见 `THIRD_PARTY.md`；`package.json` + `package-lock.json` 入库，
`node_modules/` 不入库（`/vendor/*` 白名单直接提供 npm dist，无 build step、不复制/不改三方源码）。

- **后端只读 candle 聚合**：`product/candles.py`（1m/5m/15m/1h；trades 优先，无成交的桶用盘口 mid 补足且
  `volume=0`；`source` 显式；有界 + `truncated`）；端点 `GET /api/v1/market/candles?interval=&limit=`（非法参数 400）。
- **Market K 线工作台**（`ui/pages/market/workbench.js`）：klinecharts candlestick/volume/zoom/pan/crosshair/
  tooltip/axes/last-price；timeframe 切换；指标 **MA/EMA/VOL**（库内建）+ **VWAP/ATR**（`indicators.js` 纯函数，
  renderer-independent，注册为库 indicator）；drawing toolbar 复用库/扩展 overlays（horizontalStraightLine / segment /
  `rect` / `arrow` / `measure` / `fibonacciSegment` / `fibonacciExtension`）+ 清空；**Probex semantic overlays** 由 F-08
  `evidence.trace`（canonical id + ts）生成，点击跳到 Activity/Evidence/Raw Facts；replay play/pause/step/speed/seek
  与图表时间同步；**保留原 L2 heatmap**（微观结构）与 order book / recent trades。
- **Prediction 可视化**：`PredictionView.horizons`（既有 `Prediction.future_return` 事实）+ ECharts 多 horizon 面板
  （Market + Activity）；无预测记录 ⇒ 明确 UNKNOWN，不显示中性 50%。
- **Performance**：ECharts equity / drawdown(可视化解、合同口径仍 `run_mdd`) / exposure / run metrics / compare；
  Run selector。**Monitor**：equity/PnL/exposure sparklines。**System**：latency / rate-limit / anomaly 轻量图。
- **Assistant 图表上下文**：`AssistantContext` 新增 `selected_timestamp/timeframe/candle/drawing`；UI 通过
  `ui/client/selection.js` + `/api/v1/assistant/context?timestamp=&timeframe=&candle=&drawing=` 传入。
- **真实浏览器验收**（Chrome headless + CDP，`tests/ui/verify_chart.mjs` / `capture.mjs`）：
  REPLAY 与 PAPER 均为 chartMounted/candles=181/indicators=[MA,VOL]/drawingCreated=3/drawingMoved/drawingDeleted/
  drawingRendered/zoomPan/timeframeSwitched/replaySync/assistantSelection/assistantContext PASS；
  截图 10 张在 `artifacts/ui/`（Monitor / Market K线 / drawings / semantic markers / L2 heatmap / Activity /
  prediction / Performance / System-Ops / Assistant）。
- **过程中修复的两个真实产品缺陷**：① feed provider 对 **TRADE** 事件误调 `book.on_market_event` ⇒ 任何含成交的
  event store 会直接杀死 feed（现按事件类型路由：trades 只进成交历史）；② UI 的 `queueMicrotask` 挂载早于
  `view.innerHTML` ⇒ chart/canvas/ECharts 从未真正挂载（现 console 提供 `mount()` 后置钩子）；③ 并行只读请求
  超过默认 listen backlog(5) 触发 ECONNRESET（HTTP/1.1 + `request_queue_size=128`）。
- 测试：全量 **2303 passed / 0 failed / 24 skipped**（新增 candles 单测、indicator Node 单测、candles API 集成）。
- 遗留 UX 缺口：历史 run 的 equity/candle 序列未持久化（只有当前已接线 run 有曲线/K 线）；
  prediction 面板在无预测源时显示 UNKNOWN（F6 回路未接，属后续阶段）。

## P0001.14 Runtime Decision Loop Integration（已完成，2026-10-03）

**状态**：已完成；`proposals/P0001.14-runtime-decision-loop-integration.md` 状态 = 已完成；
`context/status.json.currentProposal` = `null`（未获切换到下一阶段的授权）。

### 已完成能力

- **决策闭环真正接线**：`MarketFeedProvider` → `PredictionRuntime` → `MakerPolicy.decide(...)` → `RiskGate`（在
  `ExecutionEngine.submit` 内必经）→ readiness 记录 → `ExecutionEngine` → `OrderManager`/`OrderTracker` →
  唯一 `PaperBroker` → `AccountingCore` → Product 投影（trace/UI）。
  编排 Owner 是新增的 `runtime/decision_loop.py::RuntimeDecisionLoop`（单线程 + 自有 asyncio event loop，
  按 market state hash / prediction TTL / cadence 节流，`stop()` 不遗留线程；REPLAY = observe-only，PAPER = 可写）。
- **Risk domain canonical 预算**（裁决 §3）：新增 `risk/budget.py::remaining_exposure_budget(snapshot, limits)`，
  与 `RiskGate._increasing_exposure_checks` 语义一致（`max_position_qty` / `max_position_notional` /
  `max_open_order_exposure` / `available_balance × effective_leverage`，取最小；`mark_price` 未知或存在未量化订单
  ⇒ 未知/0，fail closed）。runtime/assembly **不做**风险数学，只消费该 provider。
- **PAPER readiness = 方案 A**（裁决 §2）：PAPER/REPLAY 的 readiness 记为 `UNAVAILABLE` +
  `PAPER_LIVE_READINESS_NOT_APPLICABLE`、`applicable=false`，trace outcome = `not_applicable`，
  **不阻断** submit，也不制造 blocker；`LiveReadinessGate` 既有规则未改动；TESTNET/LIVE 语义不变
  （本阶段 decision loop 不写入 TESTNET/LIVE；该路径仍由既有 `LiveExecutionOrchestrator` 拥有）。
- **唯一 PaperBroker**（裁决 §5）：`MarketFeedProvider` 不再构造 `PaperBroker`/`OrderManager`；
  唯一实例 = `ExecutionEngine.manager.adapter`；`runtime/decision_loop.py` 不 import 任何 venue/connector。
- **Product providers 不再是 `lambda: None`**：`prediction` / `maker_decision` / `risk_snapshot` / `risk_limits` /
  `readiness` / `prediction_fresh` / `risk_rejects` 全部投射真实 runtime 事实；
  maker 决策的真实原因（`blocked_by` 或"未报价那一侧"的 trigger）进入 trace 的 `maker_decision` stage 与
  `StrategyView.blocked_by`（无原因时如实 UNKNOWN，不伪造）。

### 实际观测（integration test 注入 provider 契约实现 + 测试参数）

- PAPER：`MakerPolicy` 自然产出初始双边 `PLACE` → `RiskGate` 逐单 `allow` → 2 张 `OPEN` 订单 + ack；
  readiness `not_applicable` 不阻断。
- 无 mark price ⇒ `remaining_risk_budget = None` ⇒ `NONE` + `RISK_BUDGET_UNKNOWN`（不伪造订单）。
- 未接线 prediction ⇒ trace `absent` + 无报价；缺 `MakerPolicy` 配置 ⇒ 无 decision（ABSENT）。

### 测试

全量 **2347 passed / 0 failed / 24 skipped**（+44：决策 loop 编排、risk budget、真实装配 PAPER/REPLAY 闭环）；
detached worktree（`7987cd5`，无 `node_modules` / 无未跟踪文件）复核同样 **2347 passed / 0 failed / 24 skipped**。

### Blocker / 待人类决定（不阻塞本阶段完成，但阻塞"产品级真实下单"）

1. **`MakerPolicyConfig` 的生产业务数值**：`profiles/trial-local.json` 不含任何 `strategy.maker.*` 键 ⇒ 产品运行
   `MakerPolicy` 为 `None`（无 decision，如实 ABSENT）。需人类给定全部 25 个参数（实现支持 `strategy.maker.*` 配置键）。
2. **prediction provider 凭据 / adverse-selection 阈值**：真实 `PredictionRuntime` 仍 `UNAVAILABLE`
   （`prediction.provider=systemone` + `prediction.adverse_selection_threshold_bps` + `prediction.timeout_ms` +
   `prediction.ttl_ms` + 环境变量 `OPENROUTER_API_KEY` 全部就位才构造）。
3. **PAPER 的 mark price 来源**：既有契约明确"不使用 last trade"，产品路径目前没有任何 mark price 注入点；
   未注入时风险事实缺失 ⇒ `RiskGate` fail closed（既有语义）。若需要，须由人类裁决 mark price 事实来源。
4. `RiskLimits` 生产限额数值（同上，全为调用方配置）。

## P0001.15 Instrument Domain + Venue Integration Contract（已完成，2026-10-03）

**状态**：已完成；`proposals/P0001.15-instrument-domain-venue-integration-contract.md` 状态 = 已完成；
`context/status.json.currentProposal` = `null`（未获切换到 P0001.16 或其它阶段的授权）。

### 已完成能力

- **Instrument Domain**（`domain/instruments/`）：正式 `InstrumentSpec`（identity / asset class / product type /
  base-quote-settle / declared 参数 / capabilities / reference price policy）+ `InstrumentRegistry`。
  当前生产实例 = `paper:BTCUSDT` / `CRYPTO` / `PERPETUAL`；EQUITY / FUTURE 只有 vocabulary（SC-19）。
  **instrument semantics 与 venue rules 分离**：执行以 venue rules（`TradingRules` / `venue.rules.*`）为准，
  instrument 的 declared 参数只做只读比对（`venue_rules_discrepancies`）。
- **Venue Integration**（`venue/`）：`VenueIdentity{venue_id, venue_type, environment}`（正交，含合法组合校验）；
  `MarketDataConnector` 与 `PrivateExecutionConnector` 两个**正交**契约（刻意不做万能 gateway）；
  两者 health 完全独立（`MarketConnectorHealth` / `PrivateConnectorHealth`，无单一 `connected`）。
- **Connector adapters**：`PaperExecutionConnector`（唯一 `PaperBroker` 的统一 execution seam：`ExecutionEngine →
  PrivateExecutionConnector → PaperExecutionConnector → PaperBroker`）、`PaperMarketDataConnector`（本地 event store
  行情事实投影）、`BinanceMarketDataConnector` / `BinancePrivateExecutionConnector`（薄 adapter，复用既有
  REST/WS/user stream/exchangeInfo/recovery/rate limit/UNKNOWN 语义，未重写 client）。
- **ReferencePrice contract**（`venue/reference_price.py`）：`PriceType{MARK,MID,INDEX,LAST}`；
  风险/会计路径**只接受** `MARK`，`LAST` 在 policy 构造期即被结构性拒绝；正式来源 = 统一事件 `MARK_PRICE`
  （Binance `markPriceUpdate` 归一化）。无正式来源 ⇒ `UNKNOWN` + reason（fail-closed），**不**回退到 last trade。
- **MARK 事实链（工程闭环完整）**：`MARK_PRICE` event → `MarketFeedProvider.reference_price_sink` →
  `MarkPriceReferenceSource` → decision loop 在**同一线程**、紧邻快照前注入 `AccountingCore.update_mark_price`
  （仅 known 时注入）→ `RiskSnapshot.mark_price` → RiskGate → PAPER execution。
- **Order ↔ Decision canonical correlation**（人类裁决 2）：`OrderCorrelation{decision_id, instrument_id, venue_id,
  prediction_id, market_state_hash}` 随 `MakerDecision → OrderProposal → Order` 进入 canonical order record；
  `OrderTracker.orders_for_decision()` 提供反查。**不存在** runtime 侧或产品侧 decision↔order 映射
  （产品层 P0001.14 的 `_decision_index` 已删除）。
- **Product / UI**：`InstrumentView` / `VenueView` / `ReferencePriceView` / 两个 `ConnectorHealth`；
  `OrderView` 增加 instrument/venue/prediction/venue_order_id；trace 每个阶段带 instrument/venue identity；
  `GET /api/v1/instrument`；`GET /api/v1/decisions/orders?decision_id=`（SC-27）；
  UI：Monitor / Market / Activity / Orders(detail) / System(instruments + connections) / Assistant(五个确定性问答)。

### 验收（人类裁决 + SC-24…SC-28）

- **SC-24 PASS**：含 `MARK_PRICE` fixture → reference price known（`source=market_event.mark_price`, price=60000）
  → Risk 允许 → 唯一 `PaperBroker` 产生 `OPEN` 订单 + ack（自然决策，非手工 smoke order）。
- **SC-25 PASS**：无 `MARK_PRICE` → `ReferencePrice UNKNOWN` → `mark_price=None` → 决策 `NONE`、零下单。
- **SC-26 PASS**：correlation 存在于订单记录；loop 与 product 均无映射（守卫断言）。
- **SC-27 PASS**：`Order → Decision` 与 `Decision → Order(s)` 双向可查（tracker + `/api/v1/decisions/orders` + UI）。
- **SC-28 PASS**：两个 connector health 独立且真实出现过不同状态。
- **验收 C PASS（离线）**：Binance 两个 connector 各自暴露 health/facts（未发真实交易）。
- **验收 F PASS**：真实浏览器 7 张截图在 `artifacts/p0001.15/`（含可见文本断言 JSON）。
- **§G 守卫 PASS**：Strategy/Risk 不 import venue/connectors；Product 不 import connectors；ExecutionEngine 不依赖
  Binance 实现；feed 不持有 broker；instrument domain 不依赖 venue；无 `VenueGateway`；`PaperBroker` 单构造点。
- 全量测试 **2401 passed / 0 failed / 24 skipped**（新增 54；连续两次通过）；
  detached worktree（`14ac3cb`，无 `node_modules`）复核同样 **2401 passed / 0 failed / 24 skipped**。

### 已知限制（不阻塞本阶段）

1. 真实 TESTNET/LIVE 写链仍归既有 `LiveExecutionOrchestrator`（本阶段未改其语义，Binance connector 只做事实投影）。
2. REPLAY/PAPER 的 MARK 取决于 event store 是否含 `MARK_PRICE` 事件；**不得**因为测试 fixture 有 MARK 就声称
   真实 Binance/PAPER 已获得实时 mark（人类裁决 1E）。
3. `INDEX` 只有 vocabulary（本阶段无正式 index price 来源）。
4. EQUITY / FUTURE 只有 vocabulary（无股票/期货/多 venue 实现，SC-19）。

## P0001.16 Venue Execution Productization（**实现中 / 未 CLOSED**，2026-10-04）

**状态**：实现中；`proposals/P0001.16-...md` 状态 = 实现中；`currentProposal = "P0001.16"`。

### 已完成（结构性）

- **TESTNET 唯一写路径组合**（`runtime/testnet.py`）：`ExecutionEngine → PrivateExecutionConnector
  (BinancePrivateExecutionConnector) → BinanceExecutionAdapter → Binance TESTNET`；`OrderTracker` 唯一订单 Owner、
  `AccountingCore` 唯一账本 Owner、`FillLedger` 追加 canonical fill、`PrivateExternalFactsProvider` 注入。
- **readiness 作为写边界权威**：生产 `ReadinessEvidenceCollector` + `LiveReadinessGate` + `issue_authority`；
  未签发 authority ⇒ adapter 拒绝（实测）。**未修改**任何 readiness / Risk 规则。
- **冷启动**：readiness `bootstrap_eligible` ⇒ 既有 `BootstrapAuthorityCoordinator` 签发 BOOTSTRAP authority（实测 ACTIVE）。
- **Acceptance capability（人类裁决 B，严格隔离）**：`execution/acceptance.py`（TESTNET/BTCUSDT/≤100/IOC only/默认关闭）
  + adapter gate（默认拒绝且不发请求）+ `submit_ioc_limit`（GTX 路径未改）；Strategy/Product/UI/Assistant/Actions
  结构性不可触达（13 条隔离测试）。
- **engine additive 真实事实 provider**：historical baseline / exchange available balance / durable HWM
  （真实资金下 daily PnL 与 drawdown 可知；默认 None 行为不变）。
- 全量 offline suite **2414 passed / 0 failed / 24 skipped**；detached worktree（`aad76f9`，无 `node_modules`）同样通过。

### 未达成 / 阻塞（导致不能 CLOSED）

- 经统一路径的 TESTNET `submit` 返回 **UNKNOWN**（`unknown_submit_count=1`，无本地拒绝），venue 无该订单
  ⇒ SC-11/SC-12/SC-13 未达成；adapter **未 retry**（语义正确）。
- **UNKNOWN 收敛缺失**：`runtime/testnet.py` 未在 UNKNOWN 后走 `query_order`/reconciliation
  ⇒ 本地订单停留 `PENDING_CREATE`，driver 的 cancel 被本地状态机正确拒绝。
- 同 endpoint/参数的 **raw** 调用成功（`orderId 28617093528 → CANCELED`）⇒ 根因在 adapter 路径的传输/时钟配置。
- venue 收尾核验 **0 挂单 / FLAT / availableBalance 4998.73**（无残余真实状态）。
- 未验证：真实 user stream 生命周期、真实 fill → ledger/accounting、真实 latency 样本、TESNET Product/UI/Assistant facts。

### 下一步

1) 定位 adapter 路径 UNKNOWN；2) 实现 UNKNOWN → query/reconciliation 收敛；3) 重跑 §14 A/B/C；
4) §15 fault injection；5) SC-16/17/18 + SC-19 guard。
