# Current State

## 当前阶段

P0001.1 – P0001.4 已完成并通过全部验收。当前无进行中的 Proposal。

## 已完成能力

### P0001.1 — Market Event + L2 Book + BookHealth

统一 `MarketEvent` 边界、载荷不变量、Binance USDⓈ-M 深度归一化、`OrderBook` 序号四判定、
`BookHealth` 四态状态机 + 重同步闭环 + `is_tradeable` 门。

### P0001.2 — Event Store + Deterministic Replay

append-only JSONL Event Store、strict 递增 ordinal、canonical JSON、内容寻址 `event_id`、
损坏输入 fail closed、`ReplayClock`、`ReplaySource`（FULL / STEP）。

### P0001.3 — MarketState + Microstructure Feature Engine

immutable `MarketState`（`market-state-v1`）、price / depth / flow / trade / returns / volatility 六段 feature、
event-domain OFI、多窗口收益与每秒已实现波动率、`DataQuality` 闸门、窗口基础设施、健康门 unavailable 语义。

### P0001.4 — Jev Prediction Runtime

- question schema `jev-market-v1`（`future_return` 4 horizons × 5 buckets + 4 个单概率问题）、canonical Jev payload、`market_state_hash`。
- payload 只含市场信息（字段白名单 + 禁止账户 / 仓位 / 盈亏 / 风险字段）。
- `PredictionRequest` 冻结输入；`Prediction` / `PredictionRecord` 不可变。
- `PredictionProvider` protocol + `JevProvider`（传输注入）+ strict parser（概率范围、分类齐备、求和解、NaN / Infinity / 未知字段全部 fail closed）。
- `PredictionScheduler`：资格闸门、limited inflight（默认 1，不排队）、stale response rejection。
- `PredictionRuntime`：async 与 Market Feed 解耦、timeout、exponential backoff 与 provider 状态、TTL 与 expired 判定、证据落盘。
- `RECORDED` / `LIVE_REQUERY` 两种 Replay 语义；RECORDED 不调用 provider。

## 进行中能力

无。

## 下一步

- 无自动授权的后续步骤；`currentProposal` 为 `null`（路线下一阶段为 P0001.5 Accounting + Risk）。
- 未包含（需人类授权后才可进行）：真实 Jev HTTP 客户端与凭证、prediction 持久化 archive、outcome / evaluation、Experiment Runtime、Strategy / Execution / Risk、第三方依赖引入（含 pytest）、性能下沉 C++/Rust。

## Blocker

无。

## 版本

Git 仓库已初始化。已提交：`5d29574`、`282ea61`（P0001.1）、P0001.2、P0001.3。
P0001.4 的实现包含在本 commit 中。
