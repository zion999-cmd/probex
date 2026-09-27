# Current State

## 当前阶段

P0001.1（Market Event + L2 Book + BookHealth）已完成并通过全部验收。当前无进行中的 Proposal。

## 已完成能力

- 统一 `MarketEvent` 边界（`venue` / `symbol` / `event_type` / `exchange_ts` / `receive_ts` / `process_ts` / `sequence` / `payload`），事件类型与载荷不匹配、序号不一致、时间戳非法均在构造时拒绝。
- `BookSnapshot` / `BookDelta` 载荷（`market/events/payloads.py`），含价格 / 数量 / 序号区间不变量。
- Binance USDⓈ-M 深度归一化（`connectors/binance/market_data/`）：REST 快照 + WS `depthUpdate` → `MarketEvent`，所有外部字段经窄化，非法报文统一抛 `MarketDataFormatError`。
- `OrderBook`（`market/book/order_book.py`）：快照应用、增量应用、档位增 / 删 / 改、best bid / best ask、top-N depth；序号判定四种结果 `APPLIED` / `ALREADY_APPLIED` / `GAP` / `AWAITING_SNAPSHOT`。
- `BookHealth` 四态状态机（`market/health/state.py`）：`AWAITING_SNAPSHOT` / `HEALTHY` / `STALE` / `RESYNCING`，含合法转换表。
- `MarketBook`（`market/book/market_book.py`）：health + 重同步缓冲 + 状态转换记录的唯一 Owner；`is_tradeable` 可交易门；gap → `STALE` + `resync_required` 信号；`request_resync()` → 新快照 → 重放缓冲增量 → `HEALTHY` 的完整闭环。
- `BookView`：携带 health 的只读盘口快照，供下游（Feature / Jev / Strategy）消费。

## 进行中能力

无。

## 下一步

- 无自动授权的后续步骤；`currentProposal` 为 `null`，等待人类指定下一个 Proposal（路线见 `context/roadmap.md`，下一阶段为 P0001.2）。
- 未包含（需人类授权后才可进行）：Live WS / REST 传输与重订阅动作、实盘连接、第三方依赖引入（含 pytest）。

## Blocker

无。

## 版本

无版本号体系（项目未初始化 Git 仓库）。
