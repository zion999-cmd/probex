# Handoff

## 当前 Proposal

P0001.2 — Event Store + Deterministic Replay：**已完成**。

`context/status.json` 中 `currentProposal` 为 `null`（无切换授权，等待人类指定下一 Proposal）。

## 本次新增

- `storage/{__init__.py,events/{__init__,codec,errors,reader,writer}.py}`
  - `codec`：`SCHEMA_VERSION=1`、`EventRecord`、canonical JSON、内容寻址 `event_id`、严格解码
  - `reader`：`EventReader` + `JsonlEventReader`（严格递增 ordinal 校验、带行号 fail closed）
  - `writer`：`EventWriter` + `JsonlEventWriter`（append-only、ordinal 续接、逐条 flush）
- `market/replay/{__init__,clock,source}.py`：`ReplayClock`（now / advance_to / reset）与 `ReplaySource`（`FULL` / `STEP`）
- 测试：`tests/unit/{test_event_codec,test_event_store,test_replay_clock}.py`、
  `tests/integration/test_event_store_replay.py`、`tests/replay/{test_replay_determinism,test_replay_isolation}.py`、
  `tests/fault/test_corrupt_event_store.py`
- `tests/support.py`：`record_for` / `record_line` / `record_raw` / `write_store` / `TempDirTestCase`

## 本次修改

- `proposals/P0001.2-event-store-deterministic-replay.md`：补齐实现契约（Included / NOT Included / 排序与时间契约 /
  SC-1..SC-10 / Acceptance Matrix），状态 已提议 → 已完成；原文保留为第 2 节
- `context/{status,current_state,handoff,roadmap,decisions}`

## 本次删除

无。

## Acceptance 结果

| SC | 结果 | 证据 |
| --- | --- | --- |
| SC-1 | PASS | `tests.unit.test_event_codec` + `tests.unit.test_event_store`：写回读回逐字段相等 |
| SC-2 | PASS | `EventStoreAppendOnlyTest`：重新打开写入端后 ordinal 续接、历史字节不变 |
| SC-3 | PASS | `tests.replay.test_replay_determinism`：两次 Replay 的 `BookUpdate` / `HealthTransition` / final `BookView` 全等 |
| SC-4 | PASS | `test_sc4_replay_gap_enters_stale`：Replay 期间 gap → `STALE`、`is_tradeable=False` |
| SC-5 | PASS | `test_sc5_resync_replay_matches_reference`：恢复后盘口与无故障参考路径逐档相等 |
| SC-6 | PASS | `tests.fault.test_corrupt_event_store`：18 类损坏输入 fail closed，且 Replay 不静默跳过 |
| SC-7 | PASS | `test_sc7_step_advances_exactly_one_event`：每次 `next_event()` 只推进一条 |
| SC-8 | PASS | `ReplayNoSleepTest`：`time.sleep` 被替换为抛错仍通过；逻辑时间跨 3 小时 |
| SC-9 | PASS | `tests.replay.test_replay_isolation`：import 白名单 + 禁止领域 |
| SC-10 | PASS | 整套测试 205 passed |

## 测试结果

- Unit: 149 passed / 0 failed
- Integration: 13 passed / 0 failed
- Fault: 27 passed / 0 failed
- Replay: 16 passed / 0 failed
- 合计：205 passed（Python 3.14.4；仅标准库）

## 风险 / 已知问题

- `JsonlEventWriter` 打开已有 store 时全量扫描续接 ordinal（O(n)）；大文件需索引或分段 store。
- 刻意保留的 ordinal 空洞被允许（便于文件切片），因此不判为损坏。
- 无损坏修复工具，统一 fail closed。
- 未实现 `LiveClock`、fsync、文件轮转、压缩、Parquet、数据库。
- 使用标准库 `unittest`（§16 未授权第三方依赖）。

## 阻塞

无。

## 下一步

等待人类指定下一个 Proposal。路线下一阶段为 P0001.3（Feature / MarketState），需先落盘独立提案。
