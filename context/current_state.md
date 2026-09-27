# Current State

## 当前阶段

P0001.1、P0001.2 已完成并通过全部验收。当前无进行中的 Proposal。

## 已完成能力

### P0001.1 — Market Event + L2 Book + BookHealth

统一 `MarketEvent` 边界（8 字段）、`BookSnapshot` / `BookDelta` 载荷不变量、Binance USDⓈ-M 深度归一化（边界 fail closed）、
`OrderBook`（档位增删改、best、top-N、序号四判定）、`BookHealth` 四态状态机 + `MarketBook` 重同步闭环 + `is_tradeable` 门。

### P0001.2 — Event Store + Deterministic Replay

- append-only JSONL Event Store（`EventWriter` / `EventReader` 接口与 adapter 分离），strict 递增 ordinal、canonical JSON。
- 内容寻址 `event_id`（sha256）用于检测历史被原地修改；损坏输入 fail closed。
- `ReplayClock`（只前进、不使用 wall-clock、不 sleep）与 `ReplaySource`（`FULL` / `STEP`）。
- Replay 消费原有 `MarketEvent`，未引入第二套事件类型；同一 store 重复 Replay 结果完全一致。

## 进行中能力

无。

## 下一步

- 无自动授权的后续步骤；`currentProposal` 为 `null`，等待人类指定下一个 Proposal（路线下一阶段为 P0001.3 Feature / MarketState）。
- 未包含（需人类授权后才可进行）：Live WS / REST 传输与重订阅、Feature / MarketState、Jev、Accounting / Risk、第三方依赖引入（含 pytest）。

## Blocker

无。

## 版本

Git 仓库已初始化。已提交：`5d29574`（协作骨架与提案目录）、`282ea61`（P0001.1）。
P0001.2 的实现包含在本 commit 中。
`CLAUDE.md` 与 `.gitignore` 被使用者全局 gitignore（`~/.gitignore_global`）排除，未纳入版本控制。
