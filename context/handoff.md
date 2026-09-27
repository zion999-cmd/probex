# Handoff

## 当前 Proposal

P0001.1 — Market Event + L2 Book + BookHealth：**已完成**。

`context/status.json` 中 `currentProposal` 为 `null`（无切换授权，等待人类指定下一 Proposal）。

## 本次新增

- `market/events/{__init__,errors,payloads,types}.py` — 统一 MarketEvent 边界
- `market/book/{__init__,errors,order_book,market_book}.py` — L2 盘口与同步状态机
- `market/health/{__init__,state}.py` — BookHealth 四态状态机
- `connectors/binance/market_data/{__init__,errors,parsing,depth}.py` — Binance 深度归一化
- `tests/{__init__,support,scenarios}.py`、`tests/unit/*`、`tests/integration/test_book_resync.py`、`tests/fault/test_sequence_faults.py`、`tests/replay/test_determinism.py`

## 本次修改

- `proposals/P0001.1-market-event-l2-book-bookhealth.md`：补齐编号说明与 P0001.1 实现契约（Included / NOT Included / SC-1..SC-6 / Acceptance Matrix），状态改为已批准；原 P0001 总架构文本作为第 2 节保留。
- `context/status.json`、`context/current_state.md`、`context/roadmap.md`、`context/decisions.md`

## 本次删除

无。

## Acceptance 结果

| SC | 结果 | 证据 |
| --- | --- | --- |
| SC-1 | PASS | `python3 -m unittest -v tests.unit.test_binance_depth` → 22 passed；其中 18 类非法报文全部抛 `MarketDataFormatError` |
| SC-2 | PASS | `python3 -m unittest -v tests.unit.test_order_book` → 15 passed（含增 / 改 / 删档、best bid/ask、top-N） |
| SC-3 | PASS | `tests.fault...SequenceFaultAcceptanceTest.test_sc3_gap_enters_stale_and_closes_gate` → PASS（`GAP` / `STALE` / `is_tradeable=False` / `resync_required=True`） |
| SC-4 | PASS | `tests.integration...BookResyncAcceptanceTest.test_sc4_resync_recovers_healthy` → PASS（恢复 `HEALTHY`，最终盘口与参考路径逐档一致） |
| SC-5 | PASS | `python3 -m unittest tests.fault.test_sequence_faults` → 7 passed |
| SC-6 | PASS | `python3 -m unittest tests.replay.test_determinism` → 3 passed（两次重放的 `BookUpdate` 序列与 final view 逐字段相等） |

## 测试结果

- Unit: 86 passed / 0 failed
- Integration: 4 passed / 0 failed
- Fault: 7 passed / 0 failed
- Replay: 3 passed / 0 failed
- 命令：`python3 -m unittest discover -s tests -t . -v` → Ran 100 tests, OK
- 运行环境：Python 3.14.4（仓库无 pyproject.toml，测试从仓库根目录直接运行）
- 依赖：仅标准库，无第三方依赖

## 风险 / 已知问题

- 未实现 Live 传输层：`parse_depth_*` 只做归一化；实际 WS / REST 连接与重订阅动作需在后续阶段实现并在 `BookUpdate.resync_required` 为真时调用 `MarketBook.request_resync()`。
- `OrderBook` 的档位排序采用「变更即失效、按需排序缓存」，每次增量后首次查询 best bid/ask 为 O(n log n)；深层盘口高频更新下需评估（P0001.3 之后）。
- `size == 0` 之外的负数量、超出交易所精度的价格等未做量级校验，仅做符号与有限性校验。
- `MarketEvent.sequence` 目前只对 book 事件强制（等于 `payload.last_update_id`），非 book 事件的语义待后续阶段定义。
- 使用标准库 `unittest` 而非 pytest，原因是 CLAUDE.md §16 未授权新增第三方依赖；后续若引入 pytest 需人类授权。

## 阻塞

无。

## 下一步

等待人类指定下一个 Proposal。若要继续路线，下一阶段为 P0001.2（Event Store + Deterministic Replay），需先由设计 Agent 落盘独立提案。
