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

`context/status.json` 的 `currentProposal` 为 `null`。

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

## 进行中能力

无。
0001.4.1 的 live 验证（SC-1 / SC-2 / SC-5）。

## 下一步

- **当前唯一授权中的步骤**：在具备 `OPENROUTER_API_KEY` 的环境中执行 `tests/live/test_openrouter_live.py`（需 `JEV_LIVE_TEST=1` opt-in），完成 SC-1 / SC-2 / SC-5；若真实 content 与 `jev-market-v1` 不一致，先报告 `CONTRACT_MISMATCH`。
- 其余未包含（需人类授权后才可进行）：prediction 持久化 archive、outcome / evaluation、Experiment Runtime、Parquet / 数据库 / 压缩、Strategy / Execution / Risk、第三方依赖引入（含 pytest）、性能下沉 C++/Rust、Live WS / REST。

## Blocker

无进行中的实现。待人类/设计决定的**业务参数**（不阻塞验收）：
0. Accounting / Risk 的限额数值（`RiskLimits` 全为调用方配置；本阶段只提供机制与默认 fail-closed 行为）；
1. adverse-selection 阈值 X（bps）——实现为必填构造参数，测试值 5.0 仅为测试参数；
2. 五分类 bucket 是否需要数值分档（当前为定性描述，与 `jev-market-v1` 既有语义一致）。
冻结项：在 Provider identity 五问全部回答前，不进入 P0001.5、不改 `jev-market-v1`、不加兼容 parser、不做 prompt engineering。

## 版本

Git 仓库已初始化，P0001.1 – P0001.4.1 均已提交（每个 commit 都能在其检出点独立通过测试）：

| commit | 阶段 | 检出后测试 |
| --- | --- | --- |
| `5d29574` | 协作骨架与提案目录 | — |
| `282ea61` | P0001.1 Market Event + L2 Book + BookHealth | 100 passed |
| `ac3975b` | P0001.2 Event Store + Deterministic Replay | 205 passed |
| `ce2bb41` | P0001.3 MarketState + Feature Engine | 344 passed |
| `c247fff` | P0001.4 Jev Prediction Runtime | 495 passed |
| `2d7d508` | P0001.4.1 OpenRouter transport | 541 passed（4 skipped） |

`CLAUDE.md` 与 `.gitignore` 被使用者全局 gitignore（`~/.gitignore_global`）排除，未纳入版本控制。
未执行 push（未获授权）。
