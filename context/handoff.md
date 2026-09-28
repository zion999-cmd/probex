# Handoff

## 当前 Proposal

P0001.4.1 — Real Jev Transport Validation：**实现中**。

`context/status.json` 中 `currentProposal` 为 `"P0001.4.1"`。

- SC-3 / SC-4 / SC-6：**PASS**（离线，stub server）。
- SC-5：**PASS**（真实调用实测 latency 已记录）。
- SC-2：**PASS（走报告分支）** —— 真实响应无法无改动进入现有 strict parser，契约差异已逐条报告（提案 §1.6）。
- SC-1：**FAIL / 阻塞** —— 未产出 `PredictionRecord`，待人类决策契约路径。

前序：P0001.1（已提交 `282ea61`）、P0001.2 / P0001.3 / P0001.4（**均未提交**，工作树中交织）。

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

## 下一步（需人类授权）

| 探测 | 内容 | 成本 | 回答 |
| --- | --- | --- | --- |
| P1 | `GET https://openrouter.ai/api/v1/models` 查 `typesafe/jev-1.13` / `typesafe/jev-router` 元数据 | 免费（只读） | Q3 部分 + Q5 |
| P2 | 单条 `noul` 探针 `POST https://openrouter.ai/api/v1/systemone`（`model=jev-1.13`） | ~2e-05，~300–550 ms | Q3 完整 + Q4 现状复核 |

两个探测都必须由人类明确授权；**不使用 `activation-room-poc/.env` 的凭证**，只使用本次会话人类提供的 OpenRouter Key。

授权后可能的后续方向（本次不实施，仅备忘）：恢复 typed `/v1/systemone` 作为 Provider；或等待 `typesafe/jev-router` 稳定；
或更换 Provider。任何方向都需要新的提案，并会决定 `jev-market-v1` 是否需要演化为 Choice/Noul 同构的 schema。
