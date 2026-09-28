## 文档状态说明（2026-09-28）

本文档是**按时间追加**的历史记录。早期 session 段落里的「未提交 / 未获授权 / 未执行 push」等表述
**已被后续 commit 推翻**，一律视为 **superseded**，不代表当前状态。

当前事实（提交与推送状态）：

| commit | 阶段 | 远端 |
| --- | --- | --- |
| `1be451a` | P0001.9.3 Startup Recovery + Account Reconciliation | 已 push 到 `origin/master` |
| `4b69a73` | P0001.9.3.1 Recovery Contract Closure | 已 push 到 `origin/master` |

判断"当前是否已提交"只以 `git log` / `git status` 为准，不引用本文档的历史句子。

## 当前 Proposal

**P0001.9.2 — Private Account + User Stream Validation：已完成（2026-09-28）。**
验收结论：**`TESTNET_PRIVATE_VALIDATED` / `MAINNET_PRIVATE_NOT_YET_VALIDATED`**；`status.json.currentProposal` 为 `null`；**P0001.9.3 不启动**（等待下一条正式 Proposal）。

人类 2026-09-28 裁决：**`KEEP_NATIVE_PRIVATE`**（不引入 ccxt / ccxt.pro，不改 public market data），P0001.9.2.1 因此**已完成**（D-032）。
**授权归因更正（人类要求）**：测试网自动下单 harness 的授权来源是**人类当前会话的明确决策（D-034）**，**不得**归因于此前的「手工成交、不新增下单代码」指令 —— 该 harness 是在本决策之后才执行的。

**门控（在这些真实项通过前，P0001.9.3 不启动）**：

- SC-4 真实 account / position snapshot
- SC-6 真实 user data stream
- SC-12 private stream latency 真实测量
- SC-13 latency gate（median 超 `max_median_private_lag_ms` ⇒ 标记 BLOCKED）

**补齐方式**（凭据只留在你自己的 shell 环境里，不写入仓库/日志/fixture）：

```bash
export BINANCE_API_KEY=...
export BINANCE_API_SECRET=...
export PROBEX_LIVE_PRIVATE=1
python3 -m unittest -v tests.live.test_binance_private_live
```

把 `PROBEX PRIVATE LIVE SMOKE REPORT` 贴回来；我会据实补 SC-4/6/12/13，再决定把 P0001.9.2 置「已完成」并把
`currentProposal → null`（届时才考虑 P0001.9.3）。

`status.json.currentProposal` = `P0001.9.2`。测试 **1318 passed / 0 failed / 17 skipped**。

## 本次新增

- `prediction/providers/openrouter.py`：`OpenRouterTransport`（真实 HTTP，标准库 `urllib.request`）、
  `build_chat_completions_body`、`extract_message_content`、`map_http_status`、`OpenRouterCall`（telemetry）
- `tests/stub_server.py`：本机 stub OpenRouter server（只监听 `127.0.0.1`）
- `tests/integration/test_openrouter_transport.py`：请求契约 / envelope 提取 / telemetry / 分层全链路 / 事件循环不阻塞
- `tests/fault/test_openrouter_transport_failures.py`：HTTP 状态映射 / timeout / envelope 失败 / 缺 Key / Key 不泄漏
- `tests/live/__init__.py`、`tests/live/contract_report.py`、`tests/live/test_openrouter_live.py`：真实调用验证（opt-in）+ 契约诊断器
- `tests/unit/test_contract_report.py`：诊断器自身的离线验证

## 本次修改

- `prediction/providers/__init__.py`、`prediction/__init__.py`：导出 OpenRouter transport 与常量
- `tests/unit/test_prediction_isolation.py`：改为「只有 openrouter.py 允许 `urllib` / `time`」的白名单模型
- `proposals/P0001.4.1-real-jev-transport-validation.md`：记录已确认外部契约、实现与验收契约、Acceptance Matrix；状态 → 实现中；原提案文本保留为第 2 节
- `context/{current_state,handoff,roadmap,decisions}`

## 本次删除

无。

## Acceptance 结果

| SC | 结果 | 证据 |
| --- | --- | --- |
| SC-1 | NOT RUN | 无 `OPENROUTER_API_KEY`；离线代理：`tests.integration.test_openrouter_transport.OpenRouterFullChainTest`（stub envelope → JevProvider → runtime → ACCEPTED record） |
| SC-2 | NOT RUN | 同上；契约诊断器离线验证通过（`tests.unit.test_contract_report` 12 passed） |
| SC-3 | PASS | `tests.fault.test_openrouter_transport_failures` 14 passed（4xx → TRANSPORT_ERROR；408/429/5xx → PROVIDER_ERROR；非 JSON / envelope 错 → INVALID_RESPONSE；timeout → TIMEOUT；全部无 record） |
| SC-4 | PASS | `...ApiKeyRedactionTest` 3 passed（异常文本 / telemetry / Record / 请求体均无 Key；Key 只在 Authorization header） |
| SC-5 | NOT RUN | 需真实 Key；离线侧已断言 telemetry 记录 `latency_ms` |
| SC-6 | PASS | `env -u OPENROUTER_API_KEY python3 -m unittest discover -s tests -t .` → 541 passed（4 skipped：live test 无 opt-in / 无 Key 时跳过且不发请求） |

## 测试结果

- Unit: 388 passed / 0 failed
- Integration: 43 passed / 0 failed
- Fault: 80 passed / 0 failed
- Replay: 26 passed / 0 failed
- Live: 4 skipped（需 `JEV_LIVE_TEST=1` + `OPENROUTER_API_KEY`）
- 合计：`python3 -m unittest discover -s tests -t .` → Ran 541 tests, OK (skipped=4)
- 运行环境：Python 3.14.4；零第三方依赖（仅标准库 `urllib.request`）

## 风险 / 已知问题

- 真实 Jev content 的形态尚未验证：旧 `fmz_v3(1).js` 的 `market_*` / `toxicity_*` / `fill_*` 与我们当前的
  `future_return.<horizon>.<category>` 五分类契约**很可能不同**。live test 一旦跑通即可给出差异；若不符，
  按提案要求先报告 `CONTRACT_MISMATCH`，再决定「在 OpenRouter adapter 做结构转换」还是「升级
  `question_schema_version`」——两者都需要人类确认，不得静默处理。
- `urllib` 阻塞调用跑在线程中：runtime 的 `asyncio.wait_for` 超时后，线程可能仍在等待 socket（受
  transport 自身 `timeout_s` 限制），其结果为孤儿但不会被使用。
- transport 不做连接复用（提案明确排除连接池）。
- 尚无预测持久化：`OPENROUTER_API_KEY` 不存在于本机环境；live 调用需人类在自己的 shell 中执行。

## 阻塞

SC-1 / SC-2 / SC-5 需要真实凭证。请在本机 shell 中执行：

```bash
export OPENROUTER_API_KEY=...   # 仅 shell 环境，不写入仓库
export JEV_LIVE_TEST=1
python3 -m unittest -v tests.live.test_openrouter_live
```

（不要把手写 Key 贴进会话；如希望我代为执行，请确保该变量已注入当前进程环境。）

## 下一步

1. 人类在具备 `OPENROUTER_API_KEY` 的环境中执行 live test（或把 Key 注入后再叫我执行）。
2. 读取 CONTRACT REPORT：确认 envelope、Jev content 的真实结构、`market_*` / `toxicity_*` / `fill_*`、
   五分类求和与 latency。
3. 若与 `jev-market-v1` 不一致：报告 `CONTRACT_MISMATCH` 及具体差异，由人类决定走 adapter 结构转换还是
   schema 升级；无论哪条路都不得修改 Prediction Runtime 上层契约。
4. SC-1/2/5 通过后，把本提案置为「已完成」，并按 §5 把 `currentProposal` 置回 `null`（无切换授权）。

## Git 历史（本次会话建立）

| commit | 阶段 | 单独检出后的测试结果 |
| --- | --- | --- |
| `5d29574` | chore：协作骨架、执行规范与提案目录 | — |
| `282ea61` | P0001.1 Market Event + L2 Book + BookHealth | 100 passed |
| `ac3975b` | P0001.2 Event Store + Deterministic Replay | 205 passed |
| `ce2bb41` | P0001.3 MarketState + Feature Engine | 344 passed |
| `c247fff` | P0001.4 Jev Prediction Runtime | 495 passed |
| `2d7d508` | P0001.4.1 OpenRouter transport（真实 Jev 接入） | 541 passed（4 skipped） |

验证方式：`git worktree add --detach <sha>` 到临时目录后运行 `python3 -m unittest discover -s tests -t .`，
每个 commit 都能独立通过测试（含其自身及之前阶段的测试）。

注意：
- `CLAUDE.md` 与 `.gitignore` 被使用者全局 gitignore（`~/.gitignore_global`）排除，未纳入版本控制。
- 未执行 `push`（未获授权）。
- 若需要把 P0001.2 – P0001.4.1 拆成更细的 commit（例如按文件再分），当前历史即为最细的**按阶段**边界。

## 2026-09-28 真实 Jev 调用结果（live test 已执行）

人类提供了 OpenRouter Key 并指路 `/Users/bx/Workspace/activation-room-poc`（内含旧 Jev 集成参考）。
用 `OPENROUTER_API_KEY=... JEV_LIVE_TEST=1 python3 -m unittest -v tests.live.test_openrouter_live` 共完成 10 次真实调用，
另用两个只读探针（`/tmp/jev_probe.py`、`/tmp/jev_probe2.py`，均在仓库外）采集content 原文形态。

结论摘要（完整证据表见提案 §1.6）：

1. **外层契约成立**：chat completions + `Bearer` + `messages[0].content = canonical payload` → 200；`choices[0].message.content` 提取正常。
2. **`typesafe/jev-router` 实际路由到 `stealth/space-bunny-alpha`（provider `Stealth`，cost 0）**，不是 TypeSafe 的 typed 端点。
3. **content 形态不稳定**：10 次里 4 次纯自然语言、2 次围栏 JSON、4 次裸 JSON（其中 1 次围栏但整体不可解析）。
4. **语义与结构脱节**：五分类 bucket 与 4 个 horizon 完全一致、样本内求和 = 1、`buy/sell_fill_probability` 命名一致，且模型确实读到了我们的 payload（回显 `market_state_hash` / OFI / microprice / spread）；
   但分布键为 `future_return_distribution`，adverse-selection 键名在两次调用间漂移，`confidence` 是分类字符串（`"low"`），每次附带不同额外字段（`reasoning`/`caveat`/`limitations`/`signals`/…）。
5. **latency 量级**：5–19 s（旧 typed `/v1/systemone` 在 `activation-room-poc/RESULTS-P3.md` 中记录为 ~270 ms/event，model `jev-1.13`，响应为 `answers.<id>.noul`）。
6. **Key 安全**：Key 只经 shell 环境传入；已核查工作树与全部 commit 中均无该 Key（SC-4 的 live 断言亦通过）。Key 目前暴露在会话记录里，**建议轮换**。

## 人类决策（2026-09-28）

执行选项 **D（Provider identity verification）**；暂不实施 C / B / A。正式结论见 `context/decisions.md` D-017 与提案 §1.6/§1.7：

- `OpenRouterTransport`: **VALIDATED**
- `OpenRouter typesafe/jev-router as hot-path JevProvider`: **REJECTED_FOR_NOW**
  （unstable model identity：实测解析为 `stealth/space-bunny-alpha`；unstable output contract：自然语言/围栏 JSON/裸 JSON 漂移；4.9–19 s latency）
- P0001.4.1 以 `CONTRACT_MISMATCH` / `PROVIDER_UNSUITABLE` 作为**有效实验结论关闭**，SC-1 豁免；**不为取得 PredictionRecord 修改系统契约**。
- 冻结（Provider identity 五问全部回答前）：不进入 P0001.5、不修改 `jev-market-v1`、不增加兼容 parser、不做 prompt engineering。

## Provider identity 核实结果（决策 D，仅用代码/文档/历史，未使用旧凭证发起请求）

来源：`/Users/bx/Workspace/activation-room-poc`（`src/gate/jev-gate.ts`、`src/config.ts`、`.env`、`README.md`、`RESULTS-P3.md`、`results/p3-jev-*.json`、`data/sessions/*.gate.jsonl`）。

| 问题 | 结论 |
| --- | --- |
| Q1 旧 ~270 ms Jev 的 endpoint | `POST https://openrouter.ai/api/v1/systemone`（OpenRouter 上的 TypeSafe native typed 端点；非直连 TypeSafe 主机） |
| Q2 provider / model | 请求 `jev-1.13` → 服务端解析 `typesafe/jev-1.13-20260917`，`provider: TypeSafe`；认证 = OpenRouter Key（`sk-or-v1-…`），成本 ~1.66e-05/次 |
| Q3 现在是否仍可用 | **现有制品无法回答**（最后可证实使用为 2026-09-21/22）；需授权探测 |
| Q4 是否提供结构化 answers 契约 | **是**：`{id, model, provider, answers:{<qid>:{type:noul|choice|score, noul?, confidence?}}, usage}`；`questions` 为 map，可一次请求 N 问；`noul` = P(yes)（无 confidence），Choice/Score 才带 confidence；旧实现记录标定探针 0.98/0.99/0.01 |
| Q5 与 `typesafe/jev-router` 的关系 | 同一 OpenRouter 平台与同一类凭证，但**不同 API 表面 + 不同上游模型**（TypeSafe typed vs `stealth/space-bunny-alpha` 文本模型）；是否同一模型家族现有证据无法判定 |

`fmz_v3(1).js` 在整个 `~/Workspace` 中不存在（已检索）；现存唯一 Jev 参考实现即 `activation-room-poc`。

## P1 / P2 探测结果（人类授权，2026-09-28）→ NATIVE_TYPED_JEV_AVAILABLE

**P1**（`GET /api/v1/models`，只读）：HTTP 200、459 ms、458 个模型。
`typesafe/jev-router` 已列出，且被标记为 **Router**（`tokenizer="Router"`、`pricing={prompt:-1,completion:-1}`、`supported_parameters=[]`、`context_length=1e6`）；
`typesafe/jev-1.13` 与 `typesafe/jev-latest` **不在**该清单。

**P2**（`POST /api/v1/systemone`，单条最小 `noul`，134 字节，仅一次）：

| 项 | 值 |
| --- | --- |
| HTTP status | 200 |
| latency | 604 ms |
| resolved model | `typesafe/jev-1.13-20260917` |
| provider | `TypeSafe` |
| response id | `gen-dec-1790554520-jlTE19blNb9oQLiQGg9n` |
| answers | `{"ok": {"type": "noul", "noul": 0.99}}` |
| usage / cost | input 277 / output 20 / 1.1634e-05 |

判定：**NATIVE_TYPED_JEV_AVAILABLE**（见 `context/decisions.md` D-018）→ 热路径 Jev 应以 `/api/v1/systemone` 为目标。

**本轮未做**：未改 Prediction Runtime、未改 `jev-market-v1`、未加兼容 parser、未重测 `typesafe/jev-router`、未进入 P0001.5、未做第二次 P2。

## 下一步（等待人类授权）

实现 typed SystemOne provider 需要**新提案**，至少要定：
1. Question schema 演化：五分类 `future_return` → **Choice（5 路）**；四个概率问题 → **Noul**；`confidence` 只存在于 Choice/Score。
2. Provider 契约：`POST {base}/v1/systemone`，body `{model, state, questions}`，响应 `answers.<qid>.{type,noul|choice|score}`。
3. 是否保留 P0001.4 的 `Prediction`/`PredictionRecord` 上层契约不变（transport 换实现，schema 升级）。

在上述授权前保持冻结：不动 `jev-market-v1`、不加兼容 parser、不做 prompt engineering、不进入 P0001.5。


## P0001.4.2 交付摘要

**新增**
- `prediction/systemone_wire.py`：wire 契约单一来源（端点 / 模型别名 / question id / Choice+Noul 构造 / state / 请求体）
- `prediction/providers/systemone.py`：`SystemOneTransport`（HTTP/Bearer/timeout/telemetry）、`SystemOneProvider`（wire ⇄ domain 适配）
- `prediction/providers/http_errors.py`：provider transport 共用的 HTTP 状态 → failure type 映射
- `prediction/parsing/systemone.py`：typed answers 严格解析 → `jev-market-v1` domain answers + provider 证据
- 测试：`tests/unit/test_systemone.py`（39）、`tests/unit/test_provider_deprecation.py`（7）、
  `tests/integration/test_systemone_provider.py`（9）、`tests/fault/test_systemone_failures.py`（15）、
  `tests/live/test_systemone_live.py`（8，opt-in）

**修改**
- `prediction/types.py`：`ProviderUsage` + `PredictionRecord` 证据字段；`prediction/providers/base.py`：`ProviderResponse` 证据字段；
  `prediction/runtime.py`：`_build_record` 透传证据（行为未变）
- `prediction/providers/__init__.py` / `prediction/__init__.py`：热路径改为 SystemOne；Chat Completions 不再导出
- `prediction/providers/openrouter.py`：标记 `DEPRECATED` + 原因（实验记录保留）
- `tests/stub_server.py`：`choice_answer` / `noul_answer` / `systemone_answers` / `systemone_envelope` + `base_url` / `systemone_endpoint`
- `tests/unit/test_prediction_isolation.py`：被授权的 transport 模块白名单加入 `systemone.py`

**Acceptance**：SC-1 – SC-10 全部 PASS（见提案 §1.5/§1.6；SC-1/2/3/4/5/9 由真实调用支撑）。

**测试**：`python3 -m unittest discover -s tests -t .` → **612 passed / 0 failed / 12 skipped**（live 需 opt-in + Key）。
真实 live 套件：3 次真实请求、8 passed、transport latency 409/462/413 ms。

**未决业务参数**（不阻塞验收）：
1. adverse-selection 阈值 X（bps）：必填构造参数，无默认值（测试值 5.0 仅为测试参数）；
2. 五分类是否需要数值分档（当前为定性描述）。

**下一步**：等待人类指定 Proposal（路线下一阶段 P0001.5 Accounting + Risk，需先落盘独立提案）。
本轮改动**未提交**（本轮未获提交授权）。


## P0001.5 交付摘要（2026-09-28）

**新增**
- `portfolio/`：`types.py`（Fill / FundingPayment / LiquidationInfo / Side）、`fills.py`（`FillLedger` + `FillOutcome`）、
  `position.py`（`Position` + `apply_fill`，含反手 close/open 拆分）、`funding.py`（`FundingLedger`）、
  `accounting.py`（`AccountingCore` + `FillApplication`）
- `risk/`：`types.py`（`OrderProposal` / `RiskSnapshot` / `RiskDecision` / `RiskReasonCode` / `ExposureClass`）、
  `limits.py`（`RiskLimits`）、`snapshot.py`（`build_risk_snapshot` / `utc_day_start_ms` / `_validate_inputs` / `_derive_drawdown`）、
  `gate.py`（`RiskGate`，硬检查 → 暴露分类 → reduce-only 放行 → increasing 限额链）
- 测试：`tests/unit/{test_fill_ledger,test_position,test_accounting,test_funding,test_risk_snapshot,test_risk_gate,test_accounting_risk_isolation}.py`、
  `tests/integration/test_accounting_risk.py`、`tests/replay/test_accounting_determinism.py`、
  `tests/fault/{test_fill_duplicates,test_risk_fail_closed}.py`
- `tests/support.py`：`make_fill` / `make_funding`

**Acceptance**：SC-1 – SC-13 全部 PASS（见提案 §2/§2.1）。

**测试**：`python3 -m unittest discover -s tests -t .` → **750 passed / 0 failed / 12 skipped**
（unit 542、integration 57、fault 107、replay 32、live 12 skipped）。

**有意为之的行为（供审查）**
1. 空仓且无 mark → 不能下单（`MISSING_MARK_PRICE`，fail closed）；
2. kill switch 第一版拒绝一切（含 reduce-only），emergency policy 待后续定义；
3. 非结算资产的 fee / funding 一律拒绝（不做多币种换算）；
4. `balance` 由账本派生而非运行式累加。

**下一步**：等待人类指定 Proposal（路线下一阶段 P0001.6 Paper Execution + Order Lifecycle，需先落盘独立提案）。
本轮改动**未提交**（本轮仅授权提交 P0001.4.2）。


## P0001.6 交付摘要（2026-09-28）

**新增**
- `execution/`：`types.py`（Order / OrderStatus / ExternalOrder / ExternalFill / 错误）、`events.py`（六类事件）、
  `tracker.py`（OrderTracker + TrackerUpdate + ExecutionEventOutcome）、`reconciliation.py`、`manager.py`、`engine.py`、
  `adapters/{base,paper}.py`
- `risk/`：`KillSwitchMode`（三态）+ `RiskLimits.kill_switch_mode` + `RiskGate` 的 REDUCE_ONLY 分支
- 测试：`tests/unit/{test_order,test_order_tracker,test_execution_events,test_paper_broker,test_execution_isolation}.py`、
  `tests/integration/{test_paper_execution,test_execution_accounting,test_execution_risk}.py`、
  `tests/fault/{test_cancel_race,test_late_fill,test_duplicate_execution,test_lost_order,test_reconciliation}.py`、
  `tests/replay/test_execution_determinism.py`、`tests/execution_support.py`

**Acceptance**：SC-1 – SC-18 全部 PASS（见提案 §2 / §2.1）。

**测试**：`python3 -m unittest discover -s tests -t .` → **867 passed / 0 failed / 12 skipped**
（unit 602、integration 80、fault 137、replay 36、live 12 skipped）。

**实施中修掉的三个真实缺陷**（由测试暴露）
1. `Order.with_status` 计算 `final_executed_quantity` 用了**转换前**的成交量；
2. `PaperBroker.cancel` 的同步 ack 又被放进 outbox → 下一次 poll 二次投递；
3. `PaperBroker.fill` 会把已 CANCELED 的外部状态改回 PARTIALLY_FILLED（late fill 复活终态）。

**下一步**：等待人类指定 Proposal（路线下一阶段 P0001.7 Market Making / P0001.8 Fill Simulation，需先落盘独立提案）。
本轮改动**未提交**（本轮仅授权提交 P0001.5）。


## P0001.6.1 交付摘要（2026-09-28）

**新增**：`tests/fault/test_uncertain_exposure.py`（15 条：SC-1 – SC-5 + 快照一致性校验）
**修改**：`execution/tracker.py`（暴露三视图 + `UnresolvedOrder`）、`execution/reconciliation.py`（adopt 失败 → 记录 unresolved；
确认/补齐 → 清除）、`execution/manager.py`、`execution/engine.py`、`risk/types.py`（快照三字段 + `UNCERTAIN_EXPOSURE_UNKNOWN`）、
`risk/snapshot.py`（三视图入参 + 自洽校验）、`risk/gate.py`（increasing-only 的 `_check_uncertain_exposure`）、
`risk/limits.py`（`enabled_checks`）、`tests/unit/test_order_tracker.py`、`tests/fault/test_lost_order.py`（旧语义断言按新提案改写）、
`tests/unit/test_risk_gate.py`（`enabled_checks` 期望）

**Acceptance**：SC-1 – SC-6 全部 PASS（见提案 §2 / §2.1）。

**测试**：`python3 -m unittest discover -s tests -t .` → **883 passed / 0 failed / 12 skipped**
（unit 603、integration 80、fault 152、replay 36、live 12 skipped）。

**下一步**：等待人类指定 Proposal（路线下一阶段 P0001.7 Market Making / P0001.8 Fill Simulation）。
本轮改动**未提交**（本轮未获提交授权）。


## P0001.7 + P0001.7.1 交付摘要（2026-09-28）

**新增（P0001.7）**：`strategy/__init__.py`、`strategy/maker/{__init__,types,pricing,sizing,inventory,lifecycle,policy}.py`；
测试 `tests/strategy_support.py` + unit(5 文件 65 条) / integration(2 文件 23 条) / fault(3 文件 24 条) / replay(2 条)。

**新增（P0001.7.1）**：`tests/fault/test_prediction_outage_reduce_only.py`（16 条）。

**修改（P0001.7.1）**：`strategy/maker/policy.py`（`PREDICTION_STALE` → `allow_new_reducing=True`；新增
`fresh_prediction()` 让不可用 prediction 完全退出方向性调整且不伪造）、`strategy/maker/__init__.py`（导出 `fresh_prediction`）、
`tests/fault/test_stale_prediction_quote_cancel.py`（+2 条，1 条按 SC-5 修正）、
`tests/integration/test_maker_paper_execution.py`（中断期出口报价连续性，按 SC-5 修正）。

**修改（未触碰业务代码）**：`proposals/P0001.7*`、`context/{current_state,handoff,decisions,roadmap,status.json}`。
**未触碰** Execution / Accounting / Prediction Runtime（SC-13 由 `tests/unit/test_strategy_isolation.py` 固定）。

**Acceptance**：P0001.7 SC-1 – SC-14 全部 PASS（SC-2 为裁决修正后的语义）；P0001.7.1 SC-1 – SC-8 全部 PASS。
人类裁决：②③ 保留；①（无 prediction 不允许新增 reduce-only）被否决并由 P0001.7.1 修正。

**测试**：`python3 -m unittest discover -s tests -t .` → **1014 passed / 0 failed / 12 skipped**
（unit 668、integration 103、fault 193、replay 38、live 12 skipped）。

**观察项（未实施，待人类裁决，见提案 P0001.7 §2）**

1. `UNKNOWN_EXPOSURE`：策略层阻断全部新增报价（含 reduce-only），但 RiskGate 只拦增加暴露的 → 两层不一致。
2. `adverse_selection_block`：有新鲜 prediction 时该侧（含 reduce-only 侧）被禁止新增报价。
3. 中断期 reduce-only 挂单可能因 SIZE_DRIFT 被 REPLACE（confidence factor 取下界所致）→ 撤单窗口内暂时没有出口报价。

**下一步**：等待人类指定 Proposal（路线下一阶段 P0001.8 Event-level Fill Simulation，需先落盘独立提案）。


## P0001.8 交付摘要（2026-09-28）

**新增**
- `execution/simulation/`：`types.py`、`queue.py`、`latency.py`、`fees.py`、`venue.py`（`SimulatedVenue`）
- `market/events/payloads.py`：`TradePayload` + `AggressorSide`；`market/events/types.py` 注册；`storage/events/codec.py` 支持 TRADE 编解码
- `tests/support.py`：`trade_event(...)`；`tests/sim_support.py`：`SimStack` + 事件构造器
- 测试 90 条：unit（`test_sim_queue` 12 / `test_sim_latency` 4 / `test_sim_fee` 5）、
  integration（`test_simulated_venue` 26 / `test_sim_execution_accounting` 7 / `test_maker_simulated_execution` 5）、
  fault（`test_sim_book_gap` 6 / `test_sim_queue_unknown` 7 / `test_sim_cancel_race` 7）、
  replay（`test_fill_sim_determinism` 6 / `test_fill_sim_no_future` 5）

**未改动**：`strategy/`、`prediction/`、`portfolio/`、`risk/`、`execution/adapters`（PaperBroker）、
execution 核心（types/tracker/manager/engine/events）、`market/book`、`market/features`。

**Acceptance**：SC-1 – SC-19 全部 PASS（见提案 §0.6 / §1）。

**测试**：`python3 -m unittest discover -s tests -t .` → **1104 passed / 0 failed / 12 skipped**
（unit 689、integration 141、fault 213、replay 49、live 12 skipped）。

**已知限制**：队列模型是 L2 近似（不声称真实 queue position）；`TRADE_THROUGH` 对多张同侧挂单各自生效；
真实 `aggTrade` 归一化与 `TradeFeatures` 接线不在本阶段。

**下一步**：等待人类指定 Proposal（路线下一阶段 P0001.9 Binance Live / P0001.10 Product API，需先落盘独立提案）。

## 人类裁决：P0001.9 拆分（2026-09-28）

原「P0001.9 Binance Live」拆为三个串行子阶段（已记入 `context/roadmap.md`）：

- **P0001.9.1 提案已落盘**（`proposals/P0001.9.1-binance-live-market-data.md`，状态 **已提议**）：
  按 CLAUDE.md §3.1 该状态**不得实现**；且 `status.json.currentProposal` 为 `null`。需要人类批准 + 明确实施指令。
- P0001.9.2 / P0001.9.3 提案未落盘。

1. **P0001.9.1 Public Market Data Live** —— 只做公网行情（REST 快照 + WS 深度/成交），不接触私钥与下单。
2. **P0001.9.2 Private Execution + User Stream** —— 真实下单/撤单 + user data stream。
3. **P0001.9.3 Startup Recovery + Account Reconciliation** —— 启动恢复与账户对账。

### 待核实的外部事实（必须在提案内钉死，不得在实现中假设）

- Binance 于 2026-03-06 公告 USDⓈ-M Futures WebSocket 路由升级：旧路径 `wss://fstream.binance.com/ws`
  与 `/stream` 计划于 2026-04-23 退役；新增分层入口 `/public`（高频：`depth`、`bookTicker`）、
  `/market`（常规：`aggTrade`、`kline`、`markPrice`、`forceOrder`）、`/private`（user data / listenKey）。
- 来源为 Binance 支持公告的镜像与多个第三方库的迁移 PR/issue（ccxt #28091、ccxt/go-binance #809、
  tiagosiebler/binance v3.5.0、unicorn-binance-websocket-api #437）。
- **不确定性（如实记录）**：尚未取得 Binance 官方开发者文档直接列出新路径的页面；
  `/public/ws` 与 `/public`、`/market/ws` 与 `/market` 的差异在来源间不一致，`/ws` 后缀是否必需未确认；
  `/public`（`@depth`/`@bookTicker`）与 `/market`（`@aggTrade`）的 stream→tier 映射在不同来源中描述有出入。
  ⇒ 提案必须先以官方文档或**人类授权的只读探测**钉死 URL 与 stream 归属，再写实现；实现方不得猜测。

### P0001.9.1 提案中仍未固定的契约点（Observation，需人类/设计方裁决后才能实现）

1. **端点 URL 未在提案中固定**：提案全文没有任何 `wss://` / REST base URL 常量，但 SC-12（真实 live smoke）
   与「长时间只读运行」都必须用到具体 URL。结合下面的 WS 迁移事实，实现方不得自行猜 URL（§12 / Contract First）。
   建议：提案内显式给出 REST base + WS 三档入口 + depth/aggTrade/markPrice 的 stream 归属，
   或授权一次只读探测把事实钉死。
2. **WS 客户端是否允许新增第三方依赖未说明**：仓库当前零第三方依赖（D-002 / §16）。
   若走标准库，需要手写握手 / 心跳 / 重连（socket + ssl）；若允许 `websockets` 之类，必须由提案明确授权。
3. **live mark price 的 Owner / 契约未定义**：提案要求「mark 独立事实、不得用 last trade 冒充」且「不改 `market-state-v1`」，
   但没说 mark 观测如何进入 `AccountingCore.update_mark_price`（谁注入、时间戳用哪个、缺失时是否 fail closed）。
   这属于新契约，需提案明确后方可实现。

### 各子阶段提案必须回答的契约问题（实现前的输入清单；P0001.9.1 已落盘者见上）

**P0001.9.1（公网行情）**

1. WS/REST 客户端形态：仓库当前零第三方依赖（§16）——是继续用标准库手写握手/重连（socket + `ssl`），
   还是由人类批准新增依赖？两者都需要提案明确。
2. 事件边界：Live 必须产出既有 `MarketEvent`（8 字段）；`sequence` / `exchange_ts` / `receive_ts` / `process_ts`
   的来源与单调性约束；`TradePayload` 是否在本阶段接入 `FeatureEngine`（会触及冻结的 `market-state-v1`，需显式决定）。
3. 断线/落库：gap 时的 `request_resync()` 触发链（新快照拉取）、重连 backoff、live→Event Store 的落盘纪律
   （决定 Replay 与 Live 是否可逐事件对齐）。
4. 时钟：live 的 wall-clock 允许出现在哪一层（P0001.4 只在 transport 允许），核心层继续禁止。
5. 验收：如何在不依赖「恰好行情发生」的前提下验证（stub server 已存在；是否可以授权有限真实连接探测）。

**P0001.9.2（私钥与下单）**

1. D-021 已声明：`ExecutionAdapter` 第一版是**同步**接口，真实 Binance 需要异步 / user stream 桥接 ——
   本阶段需要**新的契约决策**（不改 tracker 语义）。
2. 密钥纪律：只从环境变量读取、不落盘不入日志（既有纪律）；签名（HMAC-SHA256）与 timestamp/recvWindow 语义。
3. 订单映射：`client_order_id` 生成规则、`postOnly`/`reduceOnly`/`timeInForce` 映射、错误码 → `OrderRejected` 映射表。
4. listenKey 生命周期（创建 / keepalive / 失效重建）与 `ExecutionEvent` 的桥接顺序（不得二次投递）。
5. 限流（权重 / 429 / 418）与 kill switch 的交互。

**P0001.9.3（启动恢复与对账）**

1. 启动时的账户事实来源：positions / balance 是「外部事实」，不得伪造为 Fill 注入账本（否则历史盈亏被扭曲）——
   需要新的契约（例如账户基线快照）与人类对语义的裁决。
2. 与既有 `execution.reconciliation.reconcile()`、P0001.6.1 的 LOST / unresolved fail-closed 语义如何衔接。
3. 幂等与「不得静默改写历史」：重启后重复投递的成交、`event_id` / `execution_id` 去重跨进程是否持久化。
4. 无法确认的订单如何处置（是否继续占用暴露、何时允许恢复报价）。

**下一步**：人类落盘 P0001.9.1 提案后下达实施指令；在此之前保持冻结（不新增依赖、不接触网络、不改公共契约）。

## P0001.9.1 交付摘要（2026-09-28）

**新增**：`connectors/binance/market_data/{endpoints,transport,streams,trades,mark,exchange_info,snapshot,runtime}.py`；
测试 `tests/ws_stub_server.py`（本地 RFC 6455 stub）+ `tests/live_support.py`（注入式传输/HTTP）+
unit 6 文件 64 条 / integration 11 条 / fault 9 条 / live 2 条（opt-in）= 92 条。

**修改**：`market/book/market_book.py`（`invalidate`）、`market/features/engine.py`（`invalidate` + `book_health`）、
`connectors/binance/market_data/{depth,parsing,errors,__init__}.py`。

**未改动**：`portfolio/`、`risk/`、`execution/`、`strategy/`、`prediction/`（只读链路）。

**Acceptance**：SC-1 – SC-11、SC-14、SC-15 PASS（离线）；SC-12/SC-13 的**真实**部分 NOT RUN（无公网出口）；
端点假设（2026 tier 路径）**尚未被真实连接验证**。

**测试**：`python3 -m unittest discover -s tests -t .` → **1190 passed / 0 failed / 14 skipped**
（unit 753、integration 152、fault 222、replay 49、live 14 skipped）。

**下一步**：
1. 人类在具备公网出口的环境跑 live smoke（无需凭据），把真实 gap/reconnect/lag/resync 指标与端点确认补齐；
2. 据此把 P0001.9.1 置为「已完成」并把 `currentProposal` 置回 `null`；
3. 再落盘 P0001.9.2（Private Execution + User Stream）提案。

## P0001.9.1 真实公网 Acceptance 执行记录（2026-09-28）—— BLOCKED（网络层）

- 命令：`PROBEX_LIVE_SMOKE=1 python3 -m unittest -v tests.live.test_binance_live_market_data`
- 结果：`Ran 2 tests in 10.010s — FAILED (errors=1)`；无凭据检查通过；smoke 在 `runtime.connect()` 失败：
  `TransportError: cannot connect to fstream.binance.com:443: timed out`。
- 失败层：**DNS 返回非 Binance 地址（`fapi.binance.com → 162.125.2.5` Dropbox 网段、`fstream.binance.com → 128.242.245.189` Akamai 网段）
  → TCP:443 超时 → TLS / WS handshake 未进入**；HTTP `000`（`curl` exit 28）。
- 对照：`pypi.org` / `github.com` TCP:443 正常（0.1–0.23s）；`1.1.1.1`、`8.8.8.8` 与 DoH 交叉验证均不可达。
- 未伪造任何指标：depth/aggTrade/mark 计数 0，gap/reconnect/resync/malformed/duplicate 均记 0（**未发生**），
  event lag 未采样，first HEALTHY latency NOT OBSERVED，smoke duration 10.010s（仅连接阶段）。
- 未修改 parser、未 fallback、未猜 endpoint（遵守人类裁决）；P0001.9.1 保持「实现中」，`currentProposal` 保持 `P0001.9.1`，未提交。
- 下一步：在可达环境重跑同一命令，输出完整 PROBEX LIVE SMOKE REPORT（字段清单见提案 §1.1），
  通过后 SC-12/SC-13 → PASS、P0001.9.1 → Completed、`currentProposal → null`，再提交并做 detached worktree 复核。

## P0001.9.1 真实公网 Acceptance 结果（2026-09-28，已执行）

**命令/方式**：人类指定命令 + 本机出口修正（本机 DNS 对 `*.binance.com` sinkhole、TLS SNI 被 RST；
经人类自己的 Clash `127.0.0.1:7890` 隧道，仓库代码/端点/证书校验均未改）。

**结果**：SC-12 **PASS**（无凭据完成 REST snapshot + exchangeInfo + server time + depth + aggTrade + markPrice）；
SC-13 除**新鲜度**外 PASS（窗口 59.8s、depth 185 / aggTrade 580 / markPrice 26、first HEALTHY 1420ms、
snapshot latency 636ms、mark age 14–6909ms、gap 36 / resync 38 / suppressed 74、reconnect 0（未发生，不伪造）、malformed 0）；
**event lag −226 / 7267 / 18159 / 36519 ms（min/median/p95/max）不达标**。

**阻断性发现（提案 §1.3 / D-027）**：`/public` diff 深度流会跳号（`@depth` 12/32 条消息有 13–315 个缺失 id；
`@depth@100ms` 35/40；`@depth@500ms` 3/21；`@depth@250ms` 与 `/market` tier 均**无数据**）⇒
保守的 gap→resync 使 HEALTHY 占比仅 ≈14%、REST resync ≈1/s；同步 REST 抓取阻塞读循环放大 lag。
归属诊断已排除链路：只读时 lag 稳定 −224…−140ms。

**待人类裁决**：A 部分深度快照流（`@depth20@100ms`，需明确 `sequence_contiguous` 语义）／
B 有界空洞视为可接受陈旧（弱化完整性，需显式授权）／C 保持保守（研究可用、Maker 不可用）／
D 非阻塞快照抓取（仅缓解新鲜度）。

**状态**：P0001.9.1 保持「实现中」；`status.json.currentProposal` 保持 `P0001.9.1`；未提交。
裁决后我会按选项实现（含对应的离线/故障测试与验收更新），再关闭并提交。
## 诊断 P0001.9.1.1 结果（2026-09-28）：连续性判据错了，不是 Binance 丢数据

按人类指路做的只读诊断（**不改生产代码**；原始样本存 `/tmp/probex_live/sample_*.jsonl`）：

| 规则 | `btcusdt@depth`（178 对） | `btcusdt@depth@100ms`（294 对） |
| --- | --- | --- |
| `pu` 存在率 | 179/179 | 295/295 |
| **`current.pu == previous.u`** | **178/178（100%）** | **294/294（100%）** |
| `U <= prev.u+1 <= u`（D-003 现行） | **0/178** | **0/294** |
| `U == prev.u+1` | 0/178 | 0/294 |
| `pu - prev.u` | 恒 0 | 恒 0 |
| `U - prev.u` median | 116 | 120 |
| 单事件覆盖 id 数 median | 27299 | 9972 |

⇒ Futures diff 事件聚合上万 update id，`U` 与上一条 `u` 必有间距；**现行窗口判据 0% 通过 ⇒ 每条消息误判 GAP
⇒ 每秒 resync ⇒ 同步 REST 抓取阻塞读循环 ⇒ 7–36s lag / HEALTHY 占比 14%。根因是判据，不是厂商丢数、不是链路。**

**已作废**：§1.3「Binance 跳号」结论与 A/B/C/D 选项（暂停/不再需要）。
**待授权修复**：`depth.py` 解析 `pu` → `BookDeltaPayload.previous_update_id: int | None`；
`OrderBook` 优先 `previous_update_id == last_applied_u`，无 `pu` 时回退窗口规则（现货）。
这会改 **D-003 与 `BookDeltaPayload` 契约** ⇒ 需落盘提案（P0001.9.1.1 或等效）批准后实施。

**状态**：P0001.9.1 保持「实现中」；`currentProposal` 保持 `P0001.9.1`；未提交；测试 1191 passed / 0 failed / 14 skipped。

## P0001.9.1 + P0001.9.1.1 完成（2026-09-28）

- **P0001.9.1.1 修复**：`BookDeltaPayload.previous_update_id`（Futures `pu`）+ venue-aware 连续性
  （锚点「跨过快照点」`pu <= L < u`；锚点之后 `pu == last`；无 `pu` 保留窗口规则）→ 真实流 gap 36→0、resync 38→1。
- **真实公网 Acceptance（对方本机代理隧道，无凭据）**：SC-12 PASS（6 个表面）、SC-13 PASS
  （45 s 窗口、depth 179 / aggTrade 390 / markPrice 43、HEALTHY 100%、telemetry 完整、lag 有真实采样）。
- **event lag 归属**（重要）：tight-loop 实测 `/public` depth 中位 −230 ms（新鲜），而 `/market` 的
  aggTrade / markPrice 中位 2274–2790 ms ⇒ 延迟来自测试所用**代理出口排队**，不是本系统判定/处理问题。
- 两阶段均已置为「已完成」；`currentProposal` → `null`；本轮按条件授权提交（P0001.9.1 + P0001.9.1.1 同一 commit）。

## P0001.9.2.1 摘要（2026-09-28）

**代码修正（已验收）**：① `ORDER_TRADE_UPDATE` symbol 只取 `o.s`（fixture 同步改真实形态 + 3 条回归测试）；
② listenKey keepalive/create 的 HTTP 失败 ⇒ 立即 `FAILED`（不留 `RENEWING` 僵尸）；③ `stop()` best-effort 幂等
（DELETE 失败仍 `STOPPED`）；④ `stream_connected_at_ms` 改为真实 WS 连接时刻 + 新增 `snapshot_started_at_ms`。

**审计结论（只读、无凭据）**：
- `wss://ws-fapi.binance.com/ws-fapi/v1` + `userDataStream.start` 实测存在（无效 key ⇒ `401 / -2014 API-key format invalid`）；
- private tier URL 与 ccxt 4.5.84 完全一致（`/private/ws?listenKey=`，无 `events=`）；`events=` 非准入条件，投递需有效 key（待真实 smoke）；
- CCXT Pro：`watch_orders/watch_my_trades/watch_balance/watch_positions` 齐备，`info` 保留原始 payload（Probex raw 字段可取）；
  但 `parse_position` NotSupported、归一化不足需读 `info`、**user data 无连续性证据**；
- ccxt 对 futures **orderbook** 的判据包含 `pu == nonce` —— 独立佐证了 P0001.9.1.1 的修正 ✓。
- 补充（人类指路 `~/Workspace/ccxt`）：该本地 checkout 是 **v4.4.92（2026 迁移前）** —— private URL 仍是 legacy
  `/ws/<listenKey>`、无 tier 分层、无 WS API `userDataStream.*`；新版 4.5.84 才与我们的 URL 形态一致。
  两版本都含 `pu == nonce` 判据（佐证 P0001.9.1.1）。本地 checkout 与 pypi 版都**未**成为项目依赖。

**下一步**：人类在提案 §4 两个候选中裁决；随后要么继续 Native（直接进入 P0001.9.3），
要么按 §16 授权引入 ccxt 依赖并改造 private transport。

## 环境事实更新（2026-09-28）：出口与代理

- **本机有公网出口，但只能经代理**：`127.0.0.1:7890`（Clash）在听；`7893/7891/7897` 已关闭。
  出口**不稳定**：`/fapi/v1/time` 曾出现 HTTP **418（IP 限流/封禁）**、10s 超时与变慢；WS（经 relay）仍可收到真实行情。
- `ccxt_demo` **不是**"能取数据"的证据：`data/*.parquet` 全是 2025-08-16 的**历史缓存**，
  且它硬编码的代理端口 **7893 已关闭**；ccxt 4.5.84 在当前 shell `fetch_time()` 实测失败。
- 差异根因：native transport 是**裸 socket（不认代理）**；P0001.9.1 的真实验收靠
  `/tmp/probex_live/proxy_relay.py`（harness 旁路，不入仓库）跑通。ccxt 只是显式传了 `proxies`。
- 待裁决（提案 P0001.9.2.1 §3.2）：是否给 native transport **显式**加代理配置（新契约，不得静默 fallback）。
- **人类确认（2026-09-28）：代理已迁移到 `7890`**（`7893` 退役）⇒ `ccxt_demo` 里硬编码的 7893 配置已失效，
  本机一切真人网流量应走 `127.0.0.1:7890`；native 侧继续用 `/tmp/probex_live/proxy_relay.py` 旁路（不入仓库）。

## 人类裁决（2026-09-28）：P0001.9.2 真实 Acceptance 用**测试网**

- 裁决：private 真实验收改用 **Binance USDⓈ-M Futures Testnet**（零资金风险；REST `https://demo-fapi.binance.com`，
  WS `wss://fstream.binancefuture.com`；两者都经用户代理 7890 实测可达）。
- 实测（无凭据）：测试网 `/fapi/v1/time` 200、`/exchangeInfo` 741 symbols（BTCUSDT=TRADING）、
  `/public/stream?streams=btcusdt@depth` 收到真实消息；私有端点 `/fapi/v2/account`、`/fapi/v2/positionRisk`、
  `/fapi/v1/listenKey` 在无 Key 时返回 **401 = -2014（端点存在，凭证被拒）** ⇒ 测试网私有路径已就绪。
- 对照：**主网 REST 当前被 IP 封禁**（经 7890 出口 `203.10.99.11`：`418 / -1003 Way too many requests … banned until …`），
  WS 仍可用 ⇒ 主网 REST 侧的验收暂时不可行，测试网是当下正确选择。
- runner（仓库外，不改产品代码）：`/tmp/probex_live/run_private_testnet.py`
  - 用**同一套** private 代码 + 仓库 live smoke 的 `_collect` / `_build_report` / `_assert_sc13`（报告格式一致）；
  - 只改 REST base 与 WS host 指向测试网（产品端本就是可注入配置）；
  - 凭据来源：env 或 `.env` 风格文件路径（**只读入本进程环境，绝不打印/落盘**）；无凭据时 fail closed。
- **待人类提供测试网只读 Key**（`testnet.binancefuture.com` 注册 → API Key，无需入金；只需读权限）。
  拿到后我执行：`BINANCE_API_KEY=… BINANCE_API_SECRET=… python3 /tmp/probex_live/run_private_testnet.py`
  并回传 `PROBEX PRIVATE LIVE SMOKE REPORT (TESTNET)`。
- 注意（如实记录）：测试网验收证明**契约/签名/权限/生命周期**在真实 Binance 基础设施上成立；
  主网特有账户数据与主网私有链路延迟仍属未验证项（如需要，等主网 IP 封禁解除后再补一次）。

## P0001.9.2 真实测试网 Acceptance（2026-09-28，已执行）

- **凭据位置**：`~/.probex/testnet.env`（`600`，**仓库外**，仅本进程读取；值从未打印/提交）。**用完建议轮换**。
- **runner（仓库外）**：`/tmp/probex_live/run_private_testnet.py`（复用仓库 live smoke 的报告与断言；只把 REST/WS 指向测试网）。
- **结果**：签名 REST（account + positionRisk）真实通过；`server_time_offset` 185–259ms；`listenKey ACTIVE`；
  user stream 连接保持；真实 1–3 次断开 → 重连成功且 `continuity_assumed=False`（SC-10 实测）；
  `lag_basis=path_rtt`（884–1139ms < 2000ms）；malformed/duplicate/out_of_order 全 0；业务事件与心跳 ping 未出现（空账户）。
- **真实 payload 驱动的三处修正（D-033）**：`leverage` 为字符串、`marginAsset` 缺失（改由端点 + 账户 USDT 条目确认）、
  空仓 `markPrice="0"` 允许。附：传输层新增 `ping_count` → `heartbeat_count` telemetry。
- **环境发现（harness 侧）**：`/tmp` relay 之前保留 15s 空闲超时 ⇒ 静默期被 shutdown（表现为 peer closed + 频繁重连）；
  修正为隧道建立后 `settimeout(None)` 后四种 private URL 形态均可长期保持打开。
- **测试**：1325 passed / 0 failed / 17 skipped。

**要继续补真实证据（仍不改本阶段边界）**：

1. 人类在**测试网 UI** 手动下一笔小额单并撤销（我们仍只读、不下单）→ 重跑 runner 即可拿到
   `ORDER_TRADE_UPDATE` / `ACCOUNT_UPDATE` / 成交的真实事件链（SC-7/8/11/12 的真实证据）。
2. 心跳：用 `PROBEX_LIVE_SMOKE_SECONDS=360` 跑一次长窗口（ping 周期为分钟级），观察 `counts.heartbeat_pings > 0`。
3. 主网：等出口 IP 封禁（418/-1003）解除后再补一次；主网私有链路延迟与账户数据仍未验证。

## 测试网验收进展（2026-09-28）：人类的活动是 **Demo SPOT**，与本阶段范围（USDⓈ-M Futures）不同

**更正**：此前记录的「不在该 key 所属账户」判断**有误**。实测同一把 key 在 `https://demo-api.binance.com`（Demo **Spot**）上有效，
且账户里确实有人类的活动：

| 探测（同一把 key，只读） | 结果 |
| --- | --- |
| `https://demo-api.binance.com/api/v3/account` | `canTrade=true`；非零余额 `BTC 0.00000788`、`USDT 4997.711`、`USDC 5000.000` |
| `https://demo-api.binance.com/api/v3/myTrades?symbol=BTCUSDT` | **3 条成交**（最新 `isBuyer=false qty=0.0121 price=83000`，time≈几分钟前） |
| `https://demo-fapi.binance.com` / `https://testnet.binancefuture.com` 的 `/fapi/v2/account` | key 有效但 **wallet=0.00000000**、持仓/挂单/历史订单全 0 |
| `https://testnet.binance.vision`（spot testnet） | 401（与 demo 环境不同，不适用） |

⇒ 结论：人类的"模拟交易"发生在 **Demo Spot（现货）**；**Demo Spot 与 Demo USDⓈ-M Futures 是两套独立钱包/端点/事件 schema**。
P0001.9.2 的契约是 **futures**（`/fapi/v1/listenKey`、`ORDER_TRADE_UPDATE`、`ACCOUNT_UPDATE`），
故 spot 活动**无法**用来验收本阶段；spot 的事件名为 `executionReport`（`/api/v3/userDataStream`），属**另一个契约**（需独立提案，本阶段不实现）。

**要验收本阶段（二选一或都做）**：

1. **在 futures demo 里制造活动**（推荐；仍是 UI 操作，我们保持只读）：
   `demo.binance.com` → 切到「合约 / Futures」→ 把 USDT 从现货 demo 钱包划转到合约 demo 钱包 → 下小额 BTCUSDT 永续单并撤销。
   然后让我**先开 5 分钟窗口**再操作（user stream 不回溯）。
2. **若希望 Probex 支持现货**：那是新契约（spot 账户快照 + `executionReport` 归一化 + spot listenKey），
   需人类落盘独立提案后再实施（不属 P0001.9.2，本阶段不扩范围）。

## 2026-09-28：Demo 合约活动已确认（人类已在合约 demo 交易）

只读核查 `https://demo-fapi.binance.com`（同一把 key）：

| 项 | 值 |
| --- | --- |
| 合约 wallet | **4999.45778319**（available 同额，unrealized 0） |
| 非零资产 | `BTC 0.01`、`USDT 4999.45778319`、`USDC 5000` |
| 历史订单 | **3 笔 BTCUSDT MARKET**（16:04:32 SELL 0.01 FILLED、16:05:31 SELL 0.01 FILLED、16:23:02 BUY 0.02 FILLED） |
| 成交（BTCUSDT） | 3 条；最新 BUY 0.02 @82993.90，`realizedPnl=0.786`、`commission=0.66395` |
| 当前持仓 / 挂单 | 空仓 / 0 挂单（已全平） |

⇒ **SC-4 的真实数据从全 0 变为真实非零账户与成交历史**（重跑 runner 即可作为证据）。
注意：这些成交发生在任何 user stream 连接**之前** ⇒ **流事件不回溯**，SC-7/8/11/12 仍需在**流连接期间**发生订单活动。

**协议（为拿到真实流事件）**：人类在**我本次会话开启的窗口内**在期货 demo 挂一笔限价单并撤销（或成交），
我们保持只读；窗口结束后我报告 `ORDER_TRADE_UPDATE`/`ACCOUNT_UPDATE` 的字段级证据与 `private_lag_ms` 分布。

## 2026-09-28：真实事件链采集完成（实现方操作，D-034 授权）

- **harness**：`/tmp/probex_live/order_activity.py`（仓库外；单进程完成 stream 连接 + 下单 + 撤单 + 成交 + 采集 + 清理）。
- **动作**（测试网 BTCUSDT，全部小额、已撤净、结束空仓 0 挂单）：限价 BUY 0.002@80391.90 → CANCELED；
  市价 BUY 0.0008（≈66 USDT，满足 MIN_NOTIONAL）→ reduceOnly 市价 SELL 平仓。
- **采集**：8 条业务事件（`ORDER_TRADE_UPDATE` NEW/CANCELED/TRADE×2、`ACCOUNT_UPDATE`×2）；
  `commission=0.02652128/0.02652105 USDT`、`trade_id=541925860/541925876`、`reduce_only=true`（平仓单）、
  `positions` 从 `0.0008 @82879` 回到 `0.0`。
- **延迟**：samples 8 / min −30 ms / median −8 ms / p95 +2 ms / max +2 ms（`lag_basis=business_event`，阈值内）。
- **计数**：account_updates 2、order_updates 6、fills 2、duplicate 0、out_of_order 0、malformed 0、heartbeats 1、reconnects 0。
- **由真实数据修掉的两个缺陷**：① 去重键必须含 `execution_type/order_status/event_ts`（NEW→CANCELED 的 z/t 都是 0，旧规则会误判重复，D-035）；
  ② harness 数量需满足 `MIN_NOTIONAL`（50）且错误必须可见（之前静默吞掉 -1013 类错误）。
- **状态**：`currentProposal` 仍为 `P0001.9.2`（人类指示未获确认前不关闭）；本阶段 SC 除**主网**外均有真实证据。
- **已提交**：去重修正（`connectors/binance/private/events.py`）+ 相关测试 + 本轮记录。

## 后续 live deployment gate（D-036，必须遵守）

- **延迟必须做时钟校正**：真实测试网 raw event lag 出现 **−30 ms**（区间 −30 ~ +2 ms）⇒ 本地接收时钟与交易所事件时钟
  存在几十毫秒偏差，不是"负延迟"。主网验收必须使用 `event_lag_corrected = receive_ts − event_ts **+** clock_offset`
  （2026-09-28 人类裁决更正符号：`offset = 交易所 − 本地`）
  （`clock_offset` 取同一时刻的 server-time 测量），并**记录其不确定度**；否则跨环境/跨时间比较会有系统性偏差。
- **主网私有链路仍未验证**（`MAINNET_PRIVATE_NOT_YET_VALIDATED`）：需在出口可用（此前 IP 被 418 封禁）时补一次
  只读 private smoke，并对 event lag 使用上述校正口径。

## 2026-09-28：P0001.9.3 Startup Recovery + Account Reconciliation（已完成）

**当前 Proposal**：P0001.9.3（`status.json.currentProposal` 已按 CLAUDE.md §5 回到 `null`，无下一提案切换授权）。

**本次新增**

| 文件 | 内容 |
| --- | --- |
| `connectors/binance/private/orders.py` | `is_probex_order` / `parse_external_orders` / `classify_orders` / `latest_by_client_order_id` / `sort_orders_by_time`；Binance→`OrderStatus` 显式映射；缺字段/未知状态/非数组 fail closed |
| `connectors/binance/private/trades.py` | `parse_external_fills`：`userTrades` → `ExternalFill`（需 `orderId → clientOrderId` 映射）；无法归属的不 adopt，仅计 telemetry |
| `connectors/binance/private/recovery.py` | `StartupRecovery` + `RecoverySnapshot` / `RecoveryResult` / `StreamState` / `RecoveryStatus` / `RecoveryReason` / `RecoveryReadError`；`RecoveryGate`（见下） |
| `tests/unit/test_recovery_orders.py`、`tests/unit/test_accounting_baseline.py`、`tests/integration/test_startup_recovery.py`、`tests/fault/test_recovery_faults.py`、`tests/live/test_binance_recovery_live.py` | 49 条新测试（SC-1 – SC-11） |

**本次修改**

- `connectors/binance/private/rest.py`：三只读签名 GET（`open_orders` / `order_history` / `user_trades`）；
  `connectors/binance/market_data/endpoints.py`：`OPEN_ORDERS_PATH` / `ALL_ORDERS_PATH` / `USER_TRADES_PATH`。
- `portfolio/types.py` + `portfolio/accounting.py`：`ExternalAccountBaseline` 与**一次性** `bootstrap_from_baseline`
  （拒绝第二次 + 拒绝「已有 session Fill」；不产生 synthetic Fill）；`baseline` / `baseline_applied` / `historical_pnl_known` /
  `net_realized_since`；baseline 后 `peak_equity` / `drawdown` 为 `None`。
- `risk/types.py` + `risk/gate.py`：`MISSING_DRAWDOWN`（历史峰值未知的正确原因码；此前误报 `MISSING_MARK_PRICE`）。
- `execution/reconciliation.py`：`reconcile(..., external_history=...)`（终态历史收敛本地订单）；
  `_apply_status` 补齐 `avg_fill_price`（修掉「外部部分成交」RESTORE 时 `filled>0 && avg=0` 的非法中间态）。
- `tests/unit/test_private_isolation.py`：按 P0001.9.3 §0.1 **最小放宽** private 层依赖（仅 `execution.types/tracker/reconciliation`
  与 `portfolio.types/accounting`），并新增「三端点必须都是 GET」「recovery 无下单操作」两条静态/行为断言。
- `context/*`：decisions（D-037 / D-038）、roadmap（清理残留错误状态行）、current_state。

**本次删除**：无。

**Acceptance 结果**（SC-1 – SC-12 全部 PASS；矩阵见提案 §1.4）

- SC-10 **测试网真实只读验收 PASS**：`RECOVERED`、`reasons=[]`、elapsed 3476 ms；probex open orders 0 / history 5 /
  fills 2；foreign open 0；position 0.0 / BOTH；baseline_applied true、source `BINANCE_RECOVERY`、**synthetic_fills 0**、
  `historical_pnl_known=false`。runner：`/tmp/probex_live/run_recovery_testnet.py ~/.probex/testnet.env`（经本机 7890 代理 +
  仓库外 relay；凭据只从 `~/.probex/testnet.env` 注入，未打印）。
- 真实数据驱动的修正：空仓时 Binance 返回**真实行情 markPrice**（非 0）⇒ baseline 规则 = 「空仓允许 mark>0，
  `entryPrice` 必须为 0；有仓 mark 必须 >0」。

**测试结果**

- unit 876 / integration 185 / fault 256 / replay 49 passed；live 19 skipped（opt-in 关闭）。
- 全量：**1387 passed / 0 failed / 19 skipped**。
- 独立检出（`git archive HEAD` 干净检出 + 工作树叠加，未产生任何 commit；获得 commit 授权后可改用 `git worktree add --detach <sha>`）：1387 passed / 0 failed（提案 §1.5）。

**风险 / 已知问题**

1. `allOrders` / `userTrades` 受 `fact_limit` 限窗 ⇒ 窗口之外的**历史**成交不由 tracker 恢复（结果已由 baseline 覆盖）；
   `openOrders` 无窗口 ⇒ 不会漏掉任何未平挂单（D-038）。
2. 两次读取之间若恰有新成交 ⇒ `BLOCKED: BASELINE_MISMATCH`（有意的 fail-closed，需重试）。
3. 主网私有链路仍未验证（`MAINNET_PRIVATE_NOT_YET_VALIDATED`）；延迟必须按 **D-036** 做时钟校正。
4. 无 durable trading state / checkpoint（NOT Included）⇒ 每次启动都必须重走恢复流程。

**阻塞**：无。

**下一步**：`currentProposal = null`；等待人类/设计方落盘下一条正式 Proposal 后再由人类下达实施指令。
~~P0001.9.3 的代码改动尚未 commit / push~~ —— **superseded（2026-09-28）**：
P0001.9.3 已作为 commit `1be451a` 提交并 push 到 `origin/master`。

## 2026-09-28：人类裁决 D-039（下一阶段必须遵守）

- `RECOVERED` **仅**表示「当前账户/持仓/挂单/近期成交与 tracker 在当前事实边界上收敛」，
  **不是** `LIVE TRADE READY`：baseline 之后历史 PnL / peak equity / drawdown 仍是 UNKNOWN，
  `RiskGate` 继续 fail closed ⇒ 可能出现「Recovery 绿、Risk 仍不能放单」。
  下一阶段的放单许可必须有**独立** readiness 判定，不得由 `RECOVERED` 推导。
- `fact_limit` 只是 current-state 恢复窗口；baseline 只兜当前余额/持仓，不兜历史 PnL / 手续费 / funding / drawdown。
- 本条为文档级约束，**未改动** P0001.9.3 的任何代码或契约。

## 2026-09-28：P0001.9.3.1 Recovery Contract Closure（已完成）

**当前 Proposal**：P0001.9.3.1（实施中设为 `P0001.9.3.1`，完成后按 CLAUDE.md §5 回到 `null`，无下一提案切换授权）。

**本次新增**：`tests/integration/test_recovery_invalidation.py`（runtime ↔ recovery 断线接线，9 条）。
**本次修改**

- `connectors/binance/private/orders.py`：`OWNED_CLIENT_ORDER_ID_PREFIX="probex-"` + `is_probex_order` **严格匹配**
  （SC-1：`probexevil-1` / `probex2` / `probex` / `probex-` → external）。
- `connectors/binance/private/trades.py`：成交三分类 `fills` / `foreign_order_ids`(+`foreign_fill_count`) / `unresolved_order_ids`。
- `connectors/binance/private/recovery.py`：`RecoveryReason.UNRESOLVED_FILLS`；snapshot `foreign_fills` +
  `unresolved_fill_order_ids`；不可归属成交在 reconcile/baseline **之前** BLOCKED；`on_stream_discontinuity` + `bind(runtime)`。
- `connectors/binance/private/runtime.py`：`subscribe_discontinuity` / `discontinuity_events`；在 `_reconnect` /
  `_recreate_listen_key` / `stop()` 广播 reason。
- `tests/**`：unit（严格 ownership + 三分类）、fault（`UNRESOLVED_FILLS` 阻塞 / 已验证外部不阻塞）、live（报告字段 + `bind`）。
- `proposals/P0001.9.3*.md`、`context/*`（D-038 修订、D-040、roadmap、current_state）。

**本次删除**：无。

**Acceptance 结果（SC-1 – SC-7 全 PASS，矩阵见提案 §1.3）**

- **SC-3 真实测试网（只读）**：10 单 → 自有 5（`probex-audit-*`）/ 外部 5（`web_*`）；7 笔成交 → 自有 2 /
  已验证外部 5 / **不可归属 0** ⇒ `RECOVERED`（`foreign_fills=5`、`unresolved_fill_order_ids=[]`、`synthetic_fills=0`、
  `historical_pnl_known=false`）。原先被误计为 `unresolved` 的 5 笔已证明是外部订单成交。
- **SC-4**：真实断线路径（`FakeConnection.drop()` / `listenKeyExpired` / `stop()`）触发 runtime 广播 ⇒ recovery
  **自动** `NOT_RECOVERED`；测试**未**调用 `recovery.invalidate()`。
- **SC-5**：断线后 `continuity_assumed=False` ⇒ 直接 run 得 `BLOCKED`；必须 `refresh_snapshot()` 重建 boundary
  并重走完整恢复才 `RECOVERED`。

**测试结果**：unit 879 / integration 197 / fault 261 / replay 49 passed；live 19 skipped；
全量 **1405 passed / 0 failed / 19 skipped**；独立检出（`git archive HEAD` + 工作树叠加，未产生 commit）同样全 PASS。

**风险 / 已知问题**

1. 真正不可归属的成交（orderId 不在事实窗口内）会持续 `BLOCKED: UNRESOLVED_FILLS`（有意的 fail closed）；
   若要放宽必须由人类裁决「可验证证据规则」，实现方不得自行降级。
2. `fact_limit` 仍只是 current-state 窗口（D-039）。
3. `RECOVERED` ≠ `LIVE TRADE READY`（D-039）。

**阻塞**：无。
**下一步**：`currentProposal = null`；等待人类/设计方落盘下一条正式 Proposal。
~~本轮改动尚未 commit / push~~ —— **superseded（2026-09-28）**：
P0001.9.3.1 已作为 commit `4b69a73` 提交并 push 到 `origin/master`。

## 2026-09-28：P0001.9.3.2 Discontinuity Notification Reliability（已完成）

**当前 Proposal**：P0001.9.3.2（实施中设为 `P0001.9.3.2`，完成后按 CLAUDE.md §5 回到 `null`，无下一提案切换授权）。

**本次新增**：`tests/integration/test_recovery_invalidation.py::DiscontinuityNotificationReliabilityTest`（11 条）。
**本次修改**

- `connectors/binance/private/runtime.py`：`_notify_discontinuity` 故障隔离（先记录事实 → 逐 listener 独立 `try` →
  异常不外抛；只捕获 `Exception`；失败写计数 + 异常类型名）。
- `connectors/binance/private/telemetry.py`：`PrivateStreamCounters` / `PrivateStreamTelemetry` 新增
  `discontinuity_listener_failure_count`（沿用既有 counter 风格，未新建日志系统）。
- `context/current_state.md`：版本表 `4b69a73` 行 + 明确已提交并 push。
- `context/handoff.md`：顶部新增「文档状态说明」——早期段落的「未提交 / 未获授权 / 未执行 push」一律
  **superseded**（判断当前状态只以 `git log` / `git status` 为准）；P0001.9.3 / P0001.9.3.1 两处就地标注 superseded。
- `proposals/P0001.9.3.2-*.md`（§0 契约 + §1 结果）、`context/roadmap.md`、`context/decisions.md`（D-041）。

**本次删除**：无。

**Acceptance 结果（SC-1 – SC-7 全 PASS，矩阵见提案 §1.2）**

- SC-1：listener A 抛异常时 B 仍收到；**所有** listener 都抛异常时 `discontinuity_events` 仍记录事实；
  recovery 订阅者在其他 listener 崩溃时仍被自动置为 `NOT_RECOVERED`。
- SC-2/SC-3/SC-4：带崩溃 listener 的情况下，reconnect（`reconnect_count=1`、`ACTIVE`、`continuity_assumed=False`）、
  listenKey recreate（新 key 生效）、`stop()`（`STOPPED`，含未 `start()` 的情形）均正常完成，且**无异常逃逸**。
- SC-5：`discontinuity_listener_failure_count` 随每次失败累加；`last_error` 仅含异常类型名；
  异常消息里的敏感标记在 `last_error` 与整个 telemetry `repr` 中都不出现。
- 附加边界：`KeyboardInterrupt` 不被吞（observer 边界只隔离 `Exception`）。
- SC-6：`current_state` 不再声称 `1be451a` / `4b69a73` 未提交；历史段落显式 superseded。

**测试结果**：unit 879 / integration 208 / fault 261 / replay 49 passed；live 19 skipped；
全量 **1416 passed / 0 failed / 19 skipped**；独立检出（`git archive HEAD` + 工作树叠加，未产生 commit）同样全 PASS。

**风险 / 已知问题**

1. `discontinuity_events` 列表无上限（按运行时长增长），当前只用于审计/live 报告，未截断（不属本提案范围）。
2. listener 失败只保留异常类型名，不保留 reason 与 listener 身份（刻意的"最小泄漏面"）；
   若需要更强可观测性须由新提案明确字段与脱敏规则。
3. 仍然：`RECOVERED` ≠ `LIVE TRADE READY`（D-039）；主网私有链路未验证；延迟须按 D-036 时钟校正。

**阻塞**：无。
**下一步**：`currentProposal = null`；等待人类/设计方落盘下一条正式 Proposal。
~~本轮改动尚未 commit / push~~ —— **superseded（2026-09-28）**：该阶段改动已提交并 push 到 `origin/master`
（P0001.9.4 + P0001.9.4.1 = `108ab49`；P0001.9.4.1.1 = `e600e84`；P0001.9.4.2 = `2543eea`）。

## 2026-09-28：P0001.9.4 Live Readiness Gate（已完成）

**当前 Proposal**：P0001.9.4（实施中设为 `P0001.9.4`，完成后按 CLAUDE.md §5 回到 `null`，无下一提案切换授权）。

**本次新增**

- `readiness/`（新领域包，按提案"不新造 live/ 层级"的约束拍平为单层）：`__init__.py`、`types.py`、`gate.py`、`evidence.py`。
- `tests/unit/test_readiness_gate.py`（30）、`tests/unit/test_clock_calibration.py`（8）、
  `tests/integration/test_live_readiness.py`（8）、`tests/live/test_binance_readiness_live.py`（opt-in）。

**本次修改**

- `risk/snapshot.py`：`peak_equity is None` ⇒ drawdown 三元组全为 None（**修正"用当前 equity 冒充历史 peak"**）；
  新增 `exchange_available_balance` 入参（交易所权威可用余额 + age）。
- `risk/types.py`：`AvailableBalanceSource`、`ExchangeAvailableBalance`、`RiskSnapshot` 三个来源字段（默认保持本地推导）。
- `connectors/binance/private/auth.py`：`ClockCalibration` + `ServerTimeOffset.measure()` 记录完整校准。
- `connectors/binance/private/rest.py`：`measure_clock()`。
- `connectors/binance/private/telemetry.py`：corrected/raw 双延迟 + `uncorrected_lag_sample_count` + `clock_calibration`。
- `connectors/binance/private/runtime.py`：校正后延迟为主指标；`clock_calibration` / `refresh_clock_calibration()`。
- `tests/unit/test_risk_snapshot.py`、`tests/fault/test_risk_fail_closed.py`（新增 drawdown/可用余额 fail-closed 用例）；
  `tests/fault/test_private_stream_faults.py`、`tests/integration/test_private_runtime.py`（延迟断言改为 D-036 校正口径）。
- `proposals/P0001.9.4-*.md`（§0 契约 + §1 结果）、`context/{current_state,handoff,decisions,roadmap}`。

**本次删除**：无（`risk/snapshot.py` 中"peak 回退到当前 equity"的分支被删除——这正是本提案要修的缺陷）。

**Acceptance 结果（SC-1 – SC-13 全 PASS，矩阵见提案 §1.3）**

- **SC-12 真实测试网只读验收**：`recovery=recovered` + `readiness=blocked`（reasons：`PRIVATE_LATENCY_UNKNOWN`、
  `HISTORICAL_DAILY_PNL_UNKNOWN`、`HISTORICAL_DRAWDOWN_UNKNOWN`、`MARKET_NOT_READY`）；
  真实 clock offset **199 ms** / RTT **292 ms** / uncertainty **146 ms**；
  交易所 availableBalance **4998.72604447**（source `binance_account_snapshot`，age 3161 ms）；
  `peak_equity=null` / `drawdown=null` / `realized_pnl_today=null`；`synthetic_fills=0`。
- 诚实性：harness 未运行 public market-data 链（P0001.9.1 单独验收）⇒ `market_ready=false`，不伪造 green；
  窗口内无 user-stream 业务事件 ⇒ corrected lag 无样本 ⇒ `PRIVATE_LATENCY_UNKNOWN`（未知 ≠ 达标）。

**测试结果**：unit 922 / integration 216 / fault 264 / replay 49 passed；live 20 skipped；
全量 **1471 passed / 0 failed / 20 skipped**；独立检出（`git archive HEAD` + 工作树叠加，未产生 commit）同样全 PASS。

**风险 / 已知问题**

1. **历史风险重建缺失**：readiness 会持续 `HISTORICAL_*_UNKNOWN` ⇒ BLOCKED，直到独立能力补上（提案 §5 明确划出范围）。
2. **market readiness 未自动接线**：`market_ready` 由调用方显式给出（harness 传 false）；自动接入 `FeatureEngine.book_health`
   需要新提案。
3. `PRIVATE_LATENCY_UNKNOWN` 的"空样本"语义：当前视为未知（BLOCKED）；若想放宽需人类裁决。
4. `mainnet_private_validated` 目前是调用方声明的事实（主网只读验收仍未做，`MAINNET_PRIVATE_NOT_YET_VALIDATED`）。
5. 仍然：主网私有链路未验证；出口受限环境下的 clock uncertainty 会偏大（真实 146 ms）。

**阻塞**：无。
**下一步**：`currentProposal = null`；等待人类/设计方落盘下一条正式 Proposal。
~~本轮改动尚未 commit / push~~ —— **superseded（2026-09-28）**：该阶段改动已提交并 push 到 `origin/master`
（P0001.9.4 + P0001.9.4.1 = `108ab49`；P0001.9.4.1.1 = `e600e84`；P0001.9.4.2 = `2543eea`）。

## 2026-09-28：D-036 时钟校正公式符号更正（P0001.9.4 implementation correction，窄修）

**背景**：人类审计发现 P0001.9.4 实现的校正公式**符号反了**（原写成 `corrected = raw − offset`）。
`offset_ms = 交易所时钟 − 本地时钟` ⇒ 把本地接收时刻换算到交易所时钟域是 `receive_ts + offset`，
正确公式为 **`corrected = receive_ts − event_ts + offset_ms`（= `raw + offset`）**。

**修正内容（只改符号与文档，不扩范围）**

- `connectors/binance/private/auth.py`：`ClockCalibration.corrected_lag_ms` 改为 `receive_ts - event_ts + offset_ms`；
  类文档补充符号约定与反例（交易所快 200 ms、真实延迟 20 ms ⇒ raw −180 ⇒ corrected **+20**）。
- `connectors/binance/private/{telemetry,runtime}.py`：注释中的公式同步更正（行为经 `corrected_lag_ms` 单一入口）。
- 新增符号方向单测（人类指定三例）：`offset=+200 / raw=−180 → 20`、`offset=−100 / raw=+130 → 30`、
  `offset=0 / raw=25 → 25`，另加"必须是 `raw + offset` 且不等于 `raw − offset`"的回归断言。
- 既有延迟测试改为新符号口径：`tests/integration/test_private_runtime.py`（raw +500）、
  `tests/fault/test_private_stream_faults.py`（raw −490..4500 → corrected 10..5000）。
- 文档公式更正：`decisions`（D-036 就地更正 + 更正说明、D-042 §6）、`handoff`（本段 + D-036 段）、
  `current_state`、`proposals/P0001.9.2`（历史提及处就地更正并标注）、`proposals/P0001.9.4`（§0.5 修正说明 + §2 原文本就地更正并标注）。
- `uncertainty_ms` 语义不变：**单独保存**，不从 corrected lag 中加/减。
- 新增 **D-043**（记录、不阻塞）：`market_ready` 与 `mainnet_private_validated` 目前是调用方裸传的 bool / 事实，
  在真实 ExecutionAdapter 阶段必须改为受控 runtime / 验收记录生成的 typed evidence。

**测试结果**：全量 **1474 passed / 0 failed / 20 skipped**；独立检出（`git archive HEAD` + 工作树叠加）同样全 PASS。

**未做**：没有重跑测试网 runner（该窗口内无 user-stream 业务事件 ⇒ corrected lag 无样本，报告里 `corrected_median_ms` 本来就是 `null`，
重跑不会产生新的校正数据；端到端校正路径已由 integration/fault 测试覆盖）。

## 2026-09-28：P0001.9.4.1 Historical Risk Bootstrap（已完成）

**当前 Proposal**：P0001.9.4.1（实施中设为 `P0001.9.4.1`，完成后按 CLAUDE.md §5 回到 `null`）。

**本次新增**

- `connectors/binance/private/income.py`：`IncomeRow` / `IncomeHistoryCoverage` / `IncomeHistoryFacts`、
  显式分类表（TRADING 4 种 / NON_TRADING 纯钱包移动 / 其余 UNCLASSIFIED）、严格解析（空串→None）、
  `(income_type, tran_id)` 去重 + `HISTORY_CONFLICT`、`UNSUPPORTED_INCOME_ASSET`、`fetch_income_history(...)` 完整分页。
- `risk/history.py`：`HistoricalRiskBaseline`（三件事实分离；`daily_net_realized=None` = 未知）+ `compose_daily_pnl`。
- `tests/unit/test_income_history.py`（20）、`tests/integration/test_historical_risk_bootstrap.py`（14）、
  `tests/live/test_binance_income_live.py`（opt-in）。

**本次修改**

- `connectors/binance/private/rest.py`：只读 `income_history(start_time, end_time, page, limit)`（不传 symbol）。
- `connectors/binance/market_data/endpoints.py`：`INCOME_PATH`。
- `connectors/binance/private/errors.py`：`translate_market_errors` 对私有层自身错误原样透传（保留子类）。
- `risk/snapshot.py`：`build_risk_snapshot(..., historical_baseline=...)`（不传则行为不变）。
- `readiness/evidence.py`：`historical_risk_baseline_from_income(...)`（income 事实 → 领域 baseline 的纯映射；
  放在这里是为了不放松 "private 层不依赖 risk" 的架构约束）。
- `tests/live/test_binance_readiness_live.py`：`build_evidence(..., historical_baseline=...)`（报告含 baseline 区段）。
- `proposals/P0001.9.4.1-*.md`、`context/{decisions,current_state,roadmap}`。

**本次删除**：无。

**Acceptance 结果（SC-1 – SC-18 全 PASS，矩阵见提案 §1.3）**

- **SC-16 真实测试网（只读）**：coverage **complete**、pages 1、rows 11、duplicates 0；
  trading **10**（`COMMISSION`×7 + `REALIZED_PNL`×3）、non-trading **1**（`TRANSFER`）、**unclassified 0**；
  **`daily_net_realized = −1.27395553 USDT`**；assets 全为 USDT；耗时 237 ms。
- **SC-13 真实 readiness 变化**：`HISTORICAL_DAILY_PNL_UNKNOWN` **被真实解除**；
  同一轮 reasons = `PRIVATE_LATENCY_UNKNOWN` / **`HISTORICAL_DRAWDOWN_UNKNOWN`** / `MARKET_NOT_READY`；
  `peak_equity=null` / `drawdown=null`（SC-15：本阶段不伪造）。
- 真实 payload 驱动的解析修正：`TRANSFER` 行的 `symbol`/`tradeId`/`info` 是**空串**（按无值处理）。

**测试结果**：unit 945 / integration 230 / fault 264 / replay 49 passed；live 22 skipped；
全量 **1510 passed / 0 failed / 22 skipped**；独立检出（`git archive HEAD` + 工作树叠加）同样全 PASS。

**风险 / 已知问题（需要人类裁决的见提案 §1.7）**

1. `TRANSFER.tranId = 0` 不唯一 ⇒ 同一 UTC 日两笔内容不同的 TRANSFER 会 `HISTORY_CONFLICT` ⇒ BLOCKED（有意 fail closed）。
2. 返佣 / 保险清算 / 交割结算等类型目前一律 UNCLASSIFIED（⇒ BLOCKED）：是否属于 trading PnL 需人类裁决。
3. `drawdown` / `peak_equity` 仍 UNKNOWN（本阶段明确不做）；设计方建议后续 `P0001.9.4.2`。
4. D-043 的两处裸输入（`market_ready` / `mainnet_private_validated`）仍在。

**阻塞**：无。
**下一步**：`currentProposal = null`；等待人类/设计方落盘下一条正式 Proposal。
~~本轮改动尚未 commit / push~~ —— **superseded（2026-09-28）**：该阶段改动已提交并 push 到 `origin/master`
（P0001.9.4 + P0001.9.4.1 = `108ab49`；P0001.9.4.1.1 = `e600e84`；P0001.9.4.2 = `2543eea`）。

## 2026-09-28：P0001.9.4.1 implementation correction（人类裁决，窄修）

**裁决 1 —— 审计身份不得污染风险计算**：`TRANSFER.tranId = 0` 不再触发 `HISTORY_CONFLICT`。
去重身份按"是否参与 PnL"分开：TRADING 行继续用 `(income_type, tran_id)` 严格去重（冲突 ⇒ BLOCKED）；
NON_TRADING / UNCLASSIFIED 行只做**审计层**去重（完整行内容），不同内容合法共存、不影响 `daily_net_realized`。

**裁决 2 —— incomeType 白名单不扩展**：trading 仍只有 4 类；**已知非交易只有 `TRANSFER`**；
`FEE_RETURN` / `INSURANCE_CLEAR` / 交割结算 / `OPTIONS_*` / `WELCOME_BONUS` / 返佣等一律 `UNCLASSIFIED` ⇒ BLOCKED
（不凭名称猜测；等真实样本再 evidence-driven 裁决）。

**变更**：`connectors/binance/private/income.py`（`_audit_identity` + 分类型去重；`NON_TRADING_INCOME_TYPES` 收窄为 `{TRANSFER}`）；
`tests/unit/test_income_history.py::AuditIdentityRulingTest`（5 条新用例）；`proposals/P0001.9.4.1` §0.2 / §0.2.1 / §1.7 / §1.8；`context/decisions.md` D-044。

**真实回归（测试网，只读）**：`daily_net_realized = −1.27395553 USDT`、coverage complete、trading 10 / non-trading 1 / unclassified 0；
readiness reasons 不变：`PRIVATE_LATENCY_UNKNOWN` / `HISTORICAL_DRAWDOWN_UNKNOWN` / `MARKET_NOT_READY`。

**测试结果**：全量 **1515 passed / 0 failed / 22 skipped**；独立检出（`git archive HEAD` + 工作树叠加）同样全 PASS。
**未 commit / push**（未获授权）。

## 2026-09-28：P0001.9.4.1.1 Full Testnet Readiness Integration Validation（已完成）

**当前 Proposal**：P0001.9.4.1.1（完成后按 CLAUDE.md §5 回到 `null`）。

**本次新增**

- `tests/live/market_readiness.py`：public 事实 → `market_ready` 的 harness 侧映射 + `wait_for_healthy()`（测量纪律）。
- `tests/live/test_binance_full_readiness_live.py`（opt-in，只读）：完整链路 + SC 断言。
- `tests/unit/test_market_readiness_evidence.py`（7 条：12 种缺失事实全部 BLOCKED、阈值边界、报告完整性）。
- 仓库外：`/tmp/probex_live/min_order_activity.py`（LIMIT GTX 探测 + 自动数量选择 + cleanup）、
  `/tmp/probex_live/run_full_readiness_testnet.py`（全链 runner + SC 判定）。

**本次修改**：`context/current_state.md`（版本表漂移修正 SC-13）；`proposals/P0001.9.4.1.1`（§0/§1）、`context/decisions.md`（D-045）、`context/roadmap.md`。

**本次删除**：无。**未改动**：`connectors/**`、`risk/**`、`readiness/**`（不重构 D-043 的裸输入问题）。

**Acceptance 结果（SC-1 – SC-14 全 PASS）**

- **最终一次真实运行（同窗口全链）**：public `market_ready=true`（锚定 ✅、窗口内 gap/resync/malformed 0、
  aggTrade 92、state 193、mark/feed 501 ms）；private 事件 2 条 → corrected latency **157/164/171 ms**，
  raw **−104/−97/−90 ms**，**corrected − raw = 261 = offset** ✅；clock offset **261 ms** / RTT **342 ms** /
  uncertainty **171 ms**；income coverage complete + `daily_net_realized = −1.27395553 USDT`；
  活动前后 recovery 均 **RECOVERED**；最终 **position 0 / open orders 0**（errors 空）；
  **readiness = `blocked`，唯一原因 `HISTORICAL_DRAWDOWN_UNKNOWN`**。
- 三个运行如实记录：① 测量纪律缺陷（窗口起点早于锚定）② 测量纪律缺陷（窗口内 snapshot 恒为 0）+ 一次真实
  `CLOCK_UNCERTAINTY_TOO_HIGH`（uncertainty 321 > policy 300）③ 修正后只剩 drawdown。
- 授权边界遵守：仅 Testnet / 仅 BTCUSDT / notional 81.06 USDT ≤ 100 / 写操作仅在仓库外 / 未制造成交 /
  最终 flat + 0 挂单；**产品代码仍无 submit/cancel**（SC-12 由隔离测试 + 全仓 grep 固定）。

**测试结果**：unit 957 / integration 230 / fault 264 / replay 49 passed；live 24 skipped；
全量 **1524 passed / 0 failed / 24 skipped**；独立检出（`git archive HEAD` + 工作树叠加）同样全 PASS。

**风险 / 已知问题**

1. **唯一结构性 blocker = `HISTORICAL_DRAWDOWN_UNKNOWN`**；`P0001.9.4.2` 是否必要由人类依据本结论决定
   （两条路线：持久化 high-watermark vs 历史 equity reconstruction）。
2. `market_ready` 仍是 readiness 的裸 bool（D-043 约束执行阶段收紧）。
3. clock uncertainty 环境相关（实测 97–321 ms，随代理 RTT 波动）⇒ 阈值必须显式配置，且不应把"忽略 RTT"当通过。

**阻塞**：无。
**下一步**：`currentProposal = null`；等待人类决定是否落盘 `P0001.9.4.2` 或转向其他能力。
~~本轮改动尚未 commit / push~~ —— **superseded（2026-09-28）**：该阶段改动已提交并 push 到 `origin/master`
（P0001.9.4 + P0001.9.4.1 = `108ab49`；P0001.9.4.1.1 = `e600e84`；P0001.9.4.2 = `2543eea`）。

## 2026-09-28：P0001.9.4.2 Persistent Equity High-Watermark（已完成）

**当前 Proposal**：P0001.9.4.2（完成后按 CLAUDE.md §5 回到 `null`）。

**本次新增**

- `risk/high_watermark.py`：`EquityHighWatermarkState` / `HighWatermarkStatus` / `HighWatermarkScope` /
  `HighWatermarkInvalidation` / `HighWatermarkProblem` / `ActivationPreconditions` / `HighWatermarkEvidence` /
  `HighWatermarkTracker`（peak 单调 + durable-before-publish）+ 严格序列化。
- `storage/high_watermark.py`：`HighWatermarkStore`（窄接口）+ `JsonHighWatermarkStore`
  （temp → flush → **fsync** → `os.replace` → fsync 目录；`SCHEMA_VERSION` + canonical JSON + fail-closed 读取）。
- `tests/unit/test_high_watermark.py`（32）、`tests/fault/test_high_watermark_failures.py`（10）、
  `tests/integration/test_high_watermark_wiring.py`（10）、`tests/readiness_support.py`。

**本次修改**

- `risk/snapshot.py`：`build_risk_snapshot(..., high_watermark=...)`（不传 ⇒ Paper/Replay 完全不变）。
- `readiness/types.py` / `gate.py`：5 个新 reason code + `LiveReadinessEvidence.high_watermark` 显式输入 + 检查逻辑。
- `risk/__init__.py` 导出；测试 helper（readiness 各测试 + live 测试）显式传 HWM。
- `proposals/P0001.9.4.2-*.md`（§0 契约 + §1 结果）、`context/{decisions,current_state,roadmap}`。

**本次删除**：无（未复用 `AccountingCore._historical_pnl_known` 承担 durable epoch）。

**Acceptance 结果（SC-1 – SC-22 全 PASS；矩阵见提案 §1.2）**

- **SC-20 真实 Testnet 跨进程验收**：Phase A 前置条件全满足（equity diff **0.0**）→ activation 持久化；
  Phase B（**新进程**）→ 同一 activation、peak 未重置 → public ready（gap/resync 0）、2 条业务事件
  （latency median 164 ms）、clock offset 270 / uncertainty 182 ms、income daily known、无新 TRANSFER、
  recovery RECOVERED、flat + 0 挂单 ⇒ **readiness `live_ready` / scope `testnet_live_ready` / reasons `[]`**。
- **SC-6/SC-7/SC-9**：save 失败不发布新 peak；crash 后从磁盘恢复全 peak（不低估 drawdown）。
- **SC-17**：Paper/Replay 不传 HWM 时与旧路径逐字段相等。

**测试结果**：unit 997 / integration 240 / fault 274 / replay 49 passed；live 24 skipped；
全量 **1584 passed / 0 failed / 24 skipped**；独立检出（`git archive HEAD` + 工作树叠加）同样全 PASS。

**风险 / 已知问题**

1. **cash-flow-adjusted NAV 未实现**（提案明确不做）：`TRANSFER` ⇒ `INVALIDATED` + 显式 rebase；
   deposit/withdraw 既不会被当成 trading peak，也不会被当成 trading drawdown。
2. activation 目前由人类/harness 显式触发，产品尚无 activation CLI（需独立提案）。
3. `HighWatermarkEvidence` 仍是调用方传入的证据（D-043 typed-evidence 收紧属执行阶段）。
4. 本次 acceptance 中 drawdown = 0（账户自 activation 起未亏损）；**未**人为制造亏损（那需要真实资金损失，超出授权）；
   drawdown 增长路径由单测/集成测试覆盖。

**阻塞**：无。
**下一步**：`currentProposal = null`。Testnet readiness 的 blocker 集合已为空 ⇒ 下一步（Execution Readiness /
Binance ExecutionAdapter）**必须**由人类落盘新提案后再实施；不得自行启动。
~~本轮改动尚未 commit / push~~ —— **superseded（2026-09-28）**：该阶段改动已提交并 push 到 `origin/master`
（P0001.9.4 + P0001.9.4.1 = `108ab49`；P0001.9.4.1.1 = `e600e84`；P0001.9.4.2 = `2543eea`）。

## 2026-09-28：P0001.9.5 Execution Readiness Evidence Binding（已完成）

**当前 Proposal**：P0001.9.5（完成后按 CLAUDE.md §5 回到 `null`）。

**本次新增**

- `market/readiness.py`：产品侧 `MarketReadinessEvidence` / `MarketReadinessPolicy` / `build_market_evidence` / `market_ready`。
- `readiness/collector.py`：`ReadinessEvidenceCollector`（受控来源）+ `ReadinessProvenance` + `recovery_generation()`。
- `readiness/authority.py`：`ExecutionReadinessAuthority` / `issue_authority()` / `ExecutionReadinessAuthorityValidator`
  / `AuthorityInvalidReason` / `evidence_digest()` / `risk_policy_fingerprint()`。
- 测试：`tests/unit/test_readiness_authority.py`（23）、`tests/fault/test_readiness_authority_faults.py`（12）、
  `tests/integration/test_readiness_collector.py`（11）。

**本次修改**

- `readiness/types.py`：`EnvironmentValidationEvidence` / `EnvironmentValidationStatus` / `RecoveryGeneration`；
  `market` 改为 typed evidence；逐字段类型校验。
- `readiness/gate.py`：typed market/environment 检查 + 「drawdown 声称已知但 HWM 无 peak」一致性 fail closed。
- `readiness/evidence.py`：`environment_evidence(...)` 构造 typed validation。
- `connectors/binance/market_data/runtime.py`：`market_generation`（唯一入口推进 + telemetry 暴露）。
- `tests/live/market_readiness.py`：映射**委托**产品模块（单一来源）。
- 既有 readiness / harness 测试升级到 typed 契约；`proposals/P0001.9.5-*.md`（§0/§1）；`context/*`（D-047）。

**本次删除**：无（`EnvironmentEvidence.mainnet_private_validated` 由裸 bool 改为**派生判定**，构造入口改为 typed validation）。

**Acceptance 结果（SC-1 – SC-25 全 PASS；矩阵见提案 §1.2）**

- **SC-20 – SC-23 真实 Testnet 授权链**：readiness `live_ready`（reasons `[]`、digest `sha256:5f5a792c…`、
  market generation 2、HWM `testnet-act-1/1`）⇒ 签发 `testnet_live_ready` 授权（TTL 30 s）⇒ **valid**；
  真实 `runtime.stop()` ⇒ 同一授权 **invalid（RECOVERY_GENERATION_CHANGED）**；
  market generation +1 ⇒ **invalid（MARKET_GENERATION_CHANGED）**；请求 Mainnet ⇒ **invalid（SCOPE_MISMATCH）**。
  活动：notional 81.31 USDT、最终 flat + 0 挂单、2 条 `ORDER_TRADE_UPDATE`。
- **SC-1/SC-3/SC-4**：裸 `market_ready=True` 被契约拒绝；主网 validation 必须 typed 且完整（testnet 记录不转移）；
  HWM 只能来自 tracker（每次 collect 取当前证据 ⇒ digest 随 HWM 变化）。
- **SC-6**：6 类阻塞逐条证明"绝不签发授权"；**SC-18/19**：授权无任何执行能力、无 RiskGate 句柄。

**测试结果**：unit 1020 / integration 252 / fault 286 / replay 49 passed；live 24 skipped；
全量 **1631 passed / 0 failed / 24 skipped**；独立检出（`git archive HEAD` + 工作树叠加）同样全 PASS。

**风险 / 已知问题**

1. 进程内对象**没有**密码学防伪（提案明文）；机制价值是"受控来源 + 版本绑定 + TTL + 生命周期失效"。
   **TTL 无生产默认值**：`issue_authority` 的 `authority_ttl_ms` 必填；验收用的 30 s 只是测试参数
   （仓库外 harness 已改为显式必需该环境变量）。
2. activation / authority 尚无产品 CLI 入口（目前由 harness 显式触发）；主网验证记录仍靠人工/runbook。
3. `market_generation` 的推进点在 market runtime 内（connect/close/gap/resync/transport-lost）；
   若未来新增使盘口可信度变化的路径，必须复用 `_bump_market_generation`（唯一入口）。
4. 真实 ExecutionAdapter 仍未实现；实现时必须：先校验 authority（TTL + generation + scope + kill switch），
   再对每一笔订单走 fresh `RiskSnapshot` → `RiskGate`。

**阻塞**：无。
**下一步**：`currentProposal = null`；等待人类落盘 `P0001.9.6（Binance ExecutionAdapter）` 或其他提案；
不得自行启动。~~本轮改动尚未 commit / push~~ —— **superseded（2026-09-28）**：
P0001.9.5 已作为 commit `a2ab8da` 提交并 push 到 `origin/master`（1631 passed）。
