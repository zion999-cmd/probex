# Handoff

## 当前 Proposal

P0001.3 — MarketState + Microstructure Feature Engine：**已完成**。

`context/status.json` 中 `currentProposal` 为 `null`（无切换授权，等待人类指定下一 Proposal）。

## 本次新增

- `market/state/{__init__,types,quality,builder}.py`：`FEATURE_SCHEMA_VERSION = "market-state-v1"`、
  `MarketState` 与分段类型、`DataQuality` + `build_data_quality`、`build_market_state` + `compute_completeness`
- `market/features/{__init__,windows,price,depth,flow,returns,volatility,engine}.py`：
  窗口基础设施、纯函数公式层、`FlowFeaturesCalculator`、`FeatureEngine`（拥有 `MarketBook`，含 `request_resync()`）
- 测试：`tests/unit/{test_market_state,test_price_features,test_depth_features,test_ofi,test_windows,test_returns,test_volatility,test_data_quality,test_feature_layering}.py`、
  `tests/replay/test_feature_determinism.py`、`tests/fault/test_feature_health_gate.py`、`tests/integration/test_feature_engine.py`
- `tests/support.py`：`book_view` / `feature_engine` / `feed_engine`

## 本次修改

- `market/book/order_book.py`：新增 `BookMutation`、`DeltaApplication`、`apply_delta_with_mutations()`；
  `apply_delta()` 保持原契约；best bid/ask 改为 `_best_level`（max/min）
- `market/book/market_book.py`：`BookUpdate` 新增 `mutations` 字段（默认空元组）
- `market/book/__init__.py`：导出新类型
- `proposals/P0001.3-marketState-microstructure-feature-engine.md`：补齐实现契约与 Acceptance Matrix；状态 → 已完成
- `context/{status,current_state,handoff,roadmap,decisions}`

## 本次删除

无。

## Acceptance 结果

| SC | 结果 | 证据 |
| --- | --- | --- |
| SC-1 | PASS | `tests.replay.test_feature_determinism`：两次 Replay 的 MarketState 序列全等 |
| SC-2 | PASS | `tests.fault.test_feature_health_gate`：gap 后 price/depth/returns/volatility 全 None，恢复后不复活旧值 |
| SC-3 | PASS | `tests.unit.test_price_features`：对称 / 极端 / 空侧 / 零数量 |
| SC-4 | PASS | `tests.unit.test_depth_features`：公式一致、范围 [-1,1]、分母 0 → None |
| SC-5 | PASS | `tests.unit.test_ofi...test_sc5_quantity_cycle_is_visible_as_two_events`：`[+2,-2]`、`ofi_1s == 0.0` |
| SC-6 | PASS | `tests.unit.test_windows`：人工时间戳与 ReplayClock 驱动一致；不 import time |
| SC-7 | PASS | `tests.unit.test_returns`：边界包含 / 边界之后不使用 / 无历史 → None |
| SC-8 | PASS | `tests.unit.test_volatility`：1s 与 0.5s 网格结果相等 |
| SC-9 | PASS | `tests.unit.test_data_quality`：阈值边界、warm-up 全 None、观测到的 0 仍为 0 |
| SC-10 | PASS | `tests.unit.test_market_state`：schema 版本、三段冻结 |
| SC-11 | PASS | `tests.unit.test_feature_layering`：import 白名单 + 禁止领域 |
| SC-12 | PASS | 整套测试 344 passed（既有 205 条未修改） |

## 测试结果

- Unit: 269 passed / 0 failed
- Integration: 18 passed / 0 failed
- Fault: 36 passed / 0 failed
- Replay: 21 passed / 0 failed
- 合计：344 passed（Python 3.14.4；仅标准库）

## 风险 / 已知问题

- `normalized_ofi` / `VAMP_5` / 波动率量纲 / `completeness` 口径由实现固定（`decisions.md` D-006），变更必须升级 schema。
- `max_book_age_ms` 默认 `None`：本阶段没有年龄硬闸门。
- `EventWindow` 已实现并测试但无 feature 消费（D-009）。
- `best_bid` / `best_ask` 改为 O(n) 求极值；深层盘口成本为 O(n × mutations)。
- `completeness` 分母固定 45，新增 feature 字段需要升级 schema。

## 阻塞

无。

## 下一步

等待人类指定下一个 Proposal。路线下一阶段为 P0001.4（Jev Prediction Runtime），需先落盘独立提案。
