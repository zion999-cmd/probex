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

### P0001.4.1 — Real Jev Transport Validation（实现中）

- `prediction/providers/openrouter.py`：`OpenRouterTransport`（真实 HTTP，标准库 `urllib.request`）：
  OpenRouter Chat Completions 请求构造、`Authorization: Bearer`、timeout、
  `choices[0].message.content` 提取、HTTP 状态映射、调用 telemetry（request/response bytes、status、latency、error）。
- 分层保持：`OpenRouterTransport → JevProvider → 既有 strict parser`；未修改 P0001.4 上层任何契约。
- 网络与 wall-clock 能力收敛到该唯一模块（结构性测试保证）。
- 离线验证：stub server（`tests/stub_server.py`，仅 `127.0.0.1`）+ 契约诊断器（`tests/live/contract_report.py`）。
- 待完成：SC-1 / SC-2 / SC-5 需真实凭证执行 live test。

## 进行中能力

- P0001.4.1 的 live 验证（SC-1 / SC-2 / SC-5）。

## 下一步

- **当前唯一授权中的步骤**：在具备 `OPENROUTER_API_KEY` 的环境中执行 `tests/live/test_openrouter_live.py`（需 `JEV_LIVE_TEST=1` opt-in），完成 SC-1 / SC-2 / SC-5；若真实 content 与 `jev-market-v1` 不一致，先报告 `CONTRACT_MISMATCH`。
- 其余未包含（需人类授权后才可进行）：prediction 持久化 archive、outcome / evaluation、Experiment Runtime、Parquet / 数据库 / 压缩、Strategy / Execution / Risk、第三方依赖引入（含 pytest）、性能下沉 C++/Rust、Live WS / REST。

## Blocker

无进行中的实现。Provider identity 五问已全部回答（Q3 = 可用，见提案 §1.8）。
下一步实现 typed SystemOne provider **等待人类授权与新提案**（涉及 question schema 与 Choice/Noul 映射决策）。
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
