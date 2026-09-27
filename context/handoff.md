# Handoff

## 当前 Proposal

P0001.4 — Jev Prediction Runtime：**已完成**。

`context/status.json` 中 `currentProposal` 为 `null`（无切换授权，等待人类指定下一 Proposal）。

## 本次新增

- `prediction/{__init__,errors,types,runtime,scheduler}.py`：核心类型、调度层、async runtime
- `prediction/schema/market_v1.py`：`QUESTION_SCHEMA_VERSION = "jev-market-v1"`、问题定义、canonical payload、`market_state_hash`、请求构造
- `prediction/parsing/market_v1.py`：strict parser + `derived_confidence`
- `prediction/providers/{__init__,base,jev}.py`：provider 契约与 `JevProvider`（传输注入）
- `tests/fakes.py`：`FakeClock` / `FakeProvider` / 响应构造
- 测试：`tests/unit/{test_prediction_types,test_question_schema,test_jev_payload,test_prediction_parser,test_prediction_ttl,test_prediction_scheduler,test_prediction_isolation}.py`、
  `tests/integration/test_prediction_runtime.py`、`tests/fault/test_prediction_{timeout,invalid_response,out_of_order}.py`、
  `tests/replay/test_prediction_state_identity.py`
- `tests/support.py`：`warm_market_states`

## 本次修改

- `proposals/P0001.4-jev-prediction-runtime.md`：补齐实现契约与 Acceptance Matrix；状态 → 已完成
- `context/{status,current_state,handoff,roadmap,decisions}`

## 本次删除

无。

## Acceptance 结果

| SC | 结果 | 证据 |
| --- | --- | --- |
| SC-1 | PASS | 同一 MarketState 两次构造 payload / hash 字节级相同 |
| SC-2 | PASS | provider 被 gate 挡住期间 FeatureEngine 继续推进 |
| SC-3 | PASS | `tests.fault.test_prediction_out_of_order`：B 先返回 ACCEPTED、A 后返回 STALE_RESPONSE |
| SC-4 | PASS | timeout / invalid response 测试：全部 `record is None`、archive 为空 |
| SC-5 | PASS | `tests.unit.test_prediction_ttl`：expired 边界明确 |
| SC-6 | PASS | payload 字段白名单 + 无交易域依赖 |
| SC-7 | PASS | `tests.unit.test_prediction_parser`：缺分类 / NaN / Infinity / 越界全部 fail closed |
| SC-8 | PASS | `provider_confidence` 与 `derived_confidence` 来源可区分 |
| SC-9 | PASS | 记录含 hash / feature schema / question schema / provider / model / raw response |
| SC-10 | PASS | 全部测试使用 FakeProvider，无网络、无 Key |
| SC-11 | PASS | 结构性测试：预测层不 import 网络库与 wall-clock |
| SC-12 | PASS | 整套测试 495 passed（既有 344 条未修改） |

## 测试结果

- Unit: 374 passed / 0 failed
- Integration: 29 passed / 0 failed
- Fault: 66 passed / 0 failed
- Replay: 26 passed / 0 failed
- 合计：495 passed（Python 3.14.4；仅标准库）

## 风险 / 已知问题

- `JevProvider` 只有传输注入点，没有真实 HTTP 客户端（`decisions.md` D-011）。
- `derived_confidence` 公式由实现固定（D-010）；`timeout_ms` / `ttl_ms` 无默认值（D-012）。
- `InMemoryPredictionArchive` 为内存实现；持久化与 outcome / evaluation 属后续阶段。
- provider 抛出的裸异常归为 `PROVIDER_ERROR`。

## 阻塞

无。

## 下一步

等待人类指定下一个 Proposal。路线下一阶段为 P0001.5（Accounting + Risk），需先落盘独立提案。
