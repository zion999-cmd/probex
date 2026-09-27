# Current State

## 当前阶段

P0001.1、P0001.2、P0001.3 已完成并通过全部验收。当前无进行中的 Proposal。

## 已完成能力

### P0001.1 — Market Event + L2 Book + BookHealth

统一 `MarketEvent` 边界（8 字段）、载荷不变量、Binance USDⓈ-M 深度归一化、`OrderBook` 序号四判定、
`BookHealth` 四态状态机 + 重同步闭环 + `is_tradeable` 门。

### P0001.2 — Event Store + Deterministic Replay

append-only JSONL Event Store（接口与 adapter 分离）、strict 递增 ordinal、canonical JSON、内容寻址 `event_id`、
损坏输入 fail closed、`ReplayClock`、`ReplaySource`（FULL / STEP）。

### P0001.3 — MarketState + Microstructure Feature Engine

- immutable `MarketState`（schema `market-state-v1`）：identity / time / quality / price / depth / flow / trade / returns / volatility。
- instantaneous：best bid/ask、sizes、mid、spread、spread_bps、microprice。
- depth：top-N（1/5/10/20）depth、L1 / depth imbalance、VAMP_5。
- flow：event-domain OFI（基于 `BookMutation`）、滚动 OFI（1s/5s/15s）、normalized OFI、累计 mutation 计数。
- returns：1s/3s/5s/15s/30s/60s/300s（lookup 规则：latest observation ≤ target，永不读未来）。
- volatility：5s/15s/30s/60s 每秒已实现波动率（量纲 1/√s，频率无关）。
- windows：`TimeSeries` / `TimeWindow` / `EventWindow`，时间戳全为显式参数。
- `DataQuality` 闸门（book_health / history_ready / completeness / feature_ready / age_valid / tradeable / sequence_contiguous）。
- 盘口失健康时 book 派生 feature 全部显式 unavailable，OFI 窗口清空，价格历史停止写入。
- trade 域结构齐备但一律 unavailable（不伪造成交数据）。

## 进行中能力

无。

## 下一步

- 无自动授权的后续步骤；`currentProposal` 为 `null`（路线下一阶段为 P0001.4 Jev Prediction Runtime）。
- 未包含（需人类授权后才可进行）：Live WS / REST、Jev 接入、Strategy / Execution / Risk、第三方依赖引入（含 pytest）、性能下沉 C++/Rust。

## Blocker

无。

## 版本

Git 仓库已初始化。已提交：`5d29574`、`282ea61`（P0001.1）、`<本 commit 之前的 P0001.2>`。
P0001.3 的实现包含在本 commit 中。
