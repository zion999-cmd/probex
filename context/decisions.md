# Decisions

## D-001 领域优先的模块布局，不新建 `core/` 等通用桶目录

**日期**：2026-09-28
**状态**：生效
**约束来源**：CLAUDE.md §15（不得新建无明确领域归属的通用垃圾桶目录，且点名 `core/`）
**背景**：P0001 总架构文本给出的目录结构建议以 `core/market/...` 为顶层。该文本使用「建议」措辞，并非强制要求；CLAUDE.md §15 明确点名排除 `core/`。
**决策**：市场数据核心放在顶层领域包 `market/`（`market/events/`、`market/book/`、`market/health/`）；交易所接入放在 `connectors/binance/market_data/`。不创建 `core/`、`services/`、`managers/`、`utils/`。
**影响**：后续阶段的 `prediction/`、`strategy/`、`policy/`、`execution/`、`portfolio/`、`risk/`、`storage/` 等领域包同样置于顶层，不再包一层通用父目录。

## D-002 测试使用标准库 unittest，不引入 pytest

**日期**：2026-09-28
**状态**：生效（待人类许可后可变更）
**约束来源**：CLAUDE.md §16（新增第三方依赖需当前 Proposal 明确授权）、§29（最少新依赖）
**背景**：P0001 总架构授权了 `tests/` 目录结构，但未授权任何第三方依赖。P0001.1 未授权新增依赖，`unittest` 足以完成全部验收。
**决策**：P0001.1 的实现与测试零第三方依赖，使用标准库 `unittest`；测试从仓库根目录以 `python3 -m unittest discover -s tests -t .` 运行，因此暂不创建 `pyproject.toml`。
**影响**：引入 pytest 或打包配置需先获得人类授权。若后续阶段授权，测试用例的 `unittest.TestCase` 形式可被 pytest 直接收集，迁移成本低。

## D-003 盘口序号连续性以交易所 `U`/`u` 区间判定

**日期**：2026-09-28
**状态**：生效
**背景**：Binance USDⓈ-M `depthUpdate` 同时提供 `U`（首个更新 id）、`u`（末个更新 id）、`pu`（上一事件的 `u`）。
**决策**：连续性判定规则为 `first_update_id <= last_applied_update_id + 1 <= last_update_id`，不引入 `pu`；`pu` 不进入字段集。
**理由**：`U`/`u` 区间规则对现货与合约增量都成立，避免把交易所可选字段变成核心契约；同一规则同时覆盖「重复 / 过期」与「gap」两种判定。
**影响**：若将来需要更严格的连续性校验，可在归一化层增加字段，而不改动 `OrderBook` 的判定规则。

## D-004 Event Store 的排序契约落地为 recorded ordinal

**日期**：2026-09-28
**状态**：生效
**背景**：P0001.2 原文的排序契约为「1. logical stream order、2. sequence、3. recorded ordinal（最终 tie-breaker）」，但未定义三者在一个单文件 store 中的具体关系。
**决策**：单文件 store 内 ordinal 严格递增且唯一，它就是 logical stream order；Replay 顺序 = ordinal 顺序（即文件 append 顺序）。时间戳不参与排序（有专项测试）。`sequence` 不参与排序，仅由下游 `MarketBook` 做连续性判定。
**理由**：该落地方式无需引入提案未要求的额外排序字段，且完全满足「时间戳相同也能唯一确定顺序」的要求。
**影响**：跨文件合并、或需要按 `sequence` 重排的场景需新提案；`EventReader` 的严格递增校验是该契约的执行点。

## D-005 Event Store 记录完整性使用内容寻址 event_id

**日期**：2026-09-28
**状态**：生效
**背景**：提案要求记录必须含 `event_id`，且 store 必须 immutable、禁止原地修改历史。
**决策**：`event_id` = `sha256:` + canonical JSON（不含 ordinal）摘要；读取端重算并比对，不一致抛 `EventIntegrityError`。
**影响**：历史被手改可被检测；`event_id` 相同不代表同一记录（同一事件重复写入时以 ordinal 区分）。

## D-006 本阶段的 feature 固定定义（schema `market-state-v1`）

**日期**：2026-09-28
**状态**：生效
**背景**：P0001.3 要求公式 / 单位 / 窗口 / 缺失语义固定并版本化，但把若干定义留给实现（`normalized_ofi`、VAMP、波动率量纲、`completeness`）。
**决策**：
- `microprice = (bid * ask_size + ask * bid_size) / (bid_size + ask_size)`（权重为对侧数量）；分母 0 → `None`。
- `normalized_ofi_h = ofi_h / (bid_size + ask_size)`：以当前 L1 深度归一，得到与盘口规模可比的流量强度；分母缺失或 0 → `None`。
- `vamp` = VAMP_5 = 双边前 5 档的 `Σ(price*size) / Σ(size)`（仅盘口，不含成交）；总数量 0 → `None`。
- `realized_volatility_h = sqrt(Σr² / (h/1000))`，量纲「每 √秒」，因此不随 event frequency 改变量级。
- `completeness` = price/depth/flow/returns/volatility 五段中非 None 字段数 / 字段总数（45）；trade 段不计入（其可用性由 `trade_stream_available` 单独表达）。
- 窗口一律为闭区间 `[t-h, t]`，与 return 的「latest observation <= target」规则一致。
**影响**：以上任一项变化都必须把 `FEATURE_SCHEMA_VERSION` 升级（当前 `market-state-v1`）。

## D-007 盘口健康门与 tradeable 的组合

**日期**：2026-09-28
**状态**：生效
**决策**：
- `feature_ready = book_healthy AND history_ready`；`tradeable = feature_ready AND age_valid`。
- `history_ready = window_coverage_ms >= 300000`（最长 return 窗口）；因此引擎启动后需要 5 分钟事件时间才可能 `tradeable`（warm-up 语义）。
- `sequence_contiguous` 为粘性标志（一次 gap 永不复原），**不进入** `tradeable`；它只作为研究侧的数据洁净度信号。
- `max_book_age_ms` 默认 `None`（不做年龄阈值门控，只要求年龄已知）：提案未给出阈值，本实现不自行编造关键数值。
**影响**：若需要年龄硬闸门，需人类 / 设计 Agent 给出阈值。

## D-008 OrderBook 只描述 mutation，不解释 mutation

**日期**：2026-09-28
**状态**：生效
**背景**：P0001.3 §5 要求 OFI 从 sampled approximation 升级为 event-domain，且明确要求不要为了计算 OFI 把 OrderBook 变成 Feature Engine。
**决策**：新增 `BookMutation`（side / price / old_size / new_size / 变更前后最优买卖档）与 `OrderBook.apply_delta_with_mutations()`；`apply_delta()` 保持原返回契约（`DeltaOutcome`），成为前者的薄封装；`BookUpdate` 增加 `mutations` 字段（默认空元组，向后兼容）。OFI 公式只存在于 `market/features/flow.py`。
**影响**：P0001.1 的公开行为不变，既有 205 条测试无需修改。

## D-009 `EventWindow` 属提案要求的窗口基础设施，本阶段无 feature 消费

**日期**：2026-09-28
**状态**：生效（待设计 Agent 确认）
**背景**：提案设计 §6 明确「需要至少两种窗口：TimeWindow / EventWindow」，但列出的 feature 全部是时间窗。
**决策**：实现且有单元测试，本阶段没有 feature 直接消费；所有已定义 feature 使用 `TimeWindow` / `TimeSeries`。
**影响**：若设计 Agent 认为事件窗不需要，可在后续提案中移除；不建议在无消费方的情况下继续扩展它。

## D-010 `derived_confidence` 的固定定义

**日期**：2026-09-28
**状态**：生效
**背景**：P0001.4 §11 要求保留 V3 的「provider confidence 或 entropy-derived fallback」，但必须区分两者来源；提案未给出 fallback 公式。
**决策**：`provider_confidence` 取供应商响应中的 `confidence`（可缺失 → `None`）；`derived_confidence = clamp(1 - H(p_5s) / ln(5), 0, 1)`，`p_5s` 为最短 horizon（5s）的 `future_return` 分布，夹取用于消除浮点误差（均匀分布时会算出 -2.2e-16）。
**理由**：最短 horizon 是短周期决策的主要依据；归一化熵是尺度无关的确定性度量。
**影响**：若更换公式或改用其它 horizon，必须升级 `question_schema_version` 并同步更新 parser 测试。

## D-011 JevProvider 以「传输注入」形态交付，不实现 HTTP

**日期**：2026-09-28
**状态**：生效（待人类确认）
**背景**：P0001.4 的 Included 要求 `JevProvider`，但未授权任何第三方依赖，也没有 Jev endpoint / API Key 契约；NOT Included 明确不做 prompt optimization 与模型训练。
**决策**：`JevProvider(transport, model=...)` 只固化为：canonical payload → 传输函数 → 原始响应文本 + provider/model 元数据；传输层异常映射为 `PredictionTransportError`。真实 HTTP 客户端与凭证管理留待单独授权。
**影响**：本阶段的端到端验证全部通过 `tests/fakes.py` 的 `FakeProvider` 完成（SC-10），Replay 测试不可能隐式访问网络（SC-11 有结构性测试）。

## D-012 预测层的时间源与关键参数

**日期**：2026-09-28
**状态**：生效
**决策**：
- 时间一律来自构造时注入的 `Clock`（`market.replay.clock.ReplayClock` 结构上即满足）；`prediction/**` 不 import `time`、不使用 wall-clock（有结构性测试）。
- `timeout_ms` 与 `ttl_ms` 为必填构造参数，不内置默认值（提案未给出 TTL 数值，不自行编造）。
- backoff：`next_retry_at = now + min(base * 2^(n-1), cap)`，`base_backoff_ms` 默认 1000、`max_backoff_ms` 默认 60000（纯重试策略，可覆盖）。
- `max_inflight` 默认 1（提案明确建议）。
**影响**：Live 使用需要提供系统时钟实现；TTL 数值由人类 / 设计 Agent 决定。

## D-013 结果分类、archive 与 RECORDED 语义

**日期**：2026-09-28
**状态**：生效
**决策**：
- 失败分类：`TIMEOUT` / `TRANSPORT_ERROR` / `PROVIDER_ERROR` / `PARSE_ERROR` / `INVALID_RESPONSE`；空响应与语义非法归 `INVALID_RESPONSE`，JSON 语法错误与非对象 JSON 归 `PARSE_ERROR`。provider 抛出的未预期异常一律归 `PROVIDER_ERROR`。
- 跳过分类：`SKIPPED_INFLIGHT`（并发上限）、`SKIPPED_BACKOFF`（provider 退避中）、`NOT_ELIGIBLE`（资格闸门）、`NOT_RECORDED`（RECORDED 查不到历史）。
- 只有 `ACCEPTED` 的记录进入 archive；`STALE_RESPONSE` 只进内存提交日志（`runtime.submissions`），但结果对象仍携带该记录作为证据。
- `InMemoryPredictionArchive` 保留同一 `market_state_hash` 的全部记录，`find` 返回最早一条（该状态首次被预测时的原始证据）。
- RECORDED 模式不跑资格闸门、不做 staleness 判定、不调用 provider；它只做查找复现。
**影响**：持久化 archive（Parquet / 事务库）与 outcome/evaluation 属后续阶段。

## D-014 OpenRouter transport 是预测层唯一的网络与 wall-clock 边界

**日期**：2026-09-28
**状态**：生效
**背景**：P0001.4.1 确认真实 Jev 通过 OpenRouter 调用（`typesafe/jev-router`），需要真实 HTTP 与 latency 实测；而 P0001.4 曾把「预测层不 import 网络库 / 不使用 wall-clock」作为结构性约束。
**决策**：把网络与 wall-clock 能力收敛到唯一模块 `prediction/providers/openrouter.py`：
- 只使用标准库 `urllib.request`（零新增依赖）；
- `time.monotonic` 仅用于 latency telemetry；
- 结构性测试（`tests/unit/test_prediction_isolation.py`）断言「只有该模块允许 import `urllib` / `time`」，其它预测层模块与 `market/features`、`market/state` 一律禁止网络与 wall-clock。
**影响**：任何新的网络出口或 wall-clock 使用都必须先修改该白名单，从而在评审中显式可见。

## D-015 OpenRouter HTTP 状态与失败的映射表

**日期**：2026-09-28
**状态**：生效
**决策**：
- 4xx（400/401/403/404/405/406/409/410/413/415/422 及其它 4xx）→ `PredictionTransportError`（请求 / 凭证被拒，provider 未作答）；
- 408 / 425 / 429 / 5xx → `PredictionProviderError`（服务端限流或故障，交由上层 backoff）；
- 非 JSON 响应体、envelope 不符（缺 `choices` / `message` / `content` 为空）→ `PredictionInvalidResponseError`；
- HTTP / socket timeout → `PredictionTimeoutError`；
- 缺少 `OPENROUTER_API_KEY` → `PredictionTransportError`，且**不发起任何网络请求**。
**影响**：全部映射到 P0001.4 既有 failure type，runtime 无需为新 provider 增加分支。

## D-016 Jev transport 的请求/响应契约与证据语义

**日期**：2026-09-28
**状态**：生效
**背景**：人类确认了 OpenRouter 契约（请求外层 = Chat Completions，content = canonical Jev payload；响应 = `choices[0].message.content`）。
**决策**：
- 请求体固定为 `{"model": "typesafe/jev-router", "messages": [{"role": "user", "content": <canonical payload>}]}`；不额外设置 temperature 等未确认参数。
- `OpenRouterTransport(request) -> str` 返回 `choices[0].message.content`，即 strict parser 的输入。
- `PredictionRecord.raw_response` 记录 **Jev content**（不是 OpenRouter 外层）；OpenRouter 外层与 HTTP 证据保存在 transport 的 telemetry（`OpenRouterCall`），二者不混淆。
- 旧 `fmz_v3(1).js` 的 `market_*` / `toxicity_*` / `fill_*` 只作领域参考；真实响应与 `jev-market-v1` 不一致时报告 `CONTRACT_MISMATCH`，不静默 normalize、不模糊兼容。

## D-017 OpenRouter transport 通过验证；`typesafe/jev-router` 暂不采用为热路径 JevProvider

**日期**：2026-09-28
**状态**：生效（人类 2026-09-28 决策）
**决策**：
- `OpenRouterTransport`：**VALIDATED** —— 外层契约（chat completions + Bearer + `choices[0].message.content`）、HTTP 状态映射、timeout、telemetry、Key 隔离均成立。
- `OpenRouter typesafe/jev-router` 作为热路径 Jev Provider：**REJECTED_FOR_NOW**。
  Reason: unstable model identity（实测解析为 `stealth/space-bunny-alpha` / provider `Stealth`）、
  unstable output contract（自然语言 / 围栏 JSON / 裸 JSON 漂移，键名与额外字段随调用变化）、
  4.9–19 s observed latency。
- **不以** prompt 强制 JSON、schema 放宽或 adapter 模糊转换来掩盖该事实；`jev-market-v1` 保持不变。
- P0001.4.1 以 `CONTRACT_MISMATCH` / `PROVIDER_UNSUITABLE` 作为**有效实验结论**关闭；SC-1 不要求通过。
- 在 Provider identity 的五个问题全部回答前：不进入 P0001.5、不修改 `jev-market-v1`、不增加兼容 parser、不做 prompt engineering。
**事后核实（决策 D，见提案 §1.7）**：旧 ~270 ms Jev 走的是同一 OpenRouter 平台的另一个 API 表面 ——
`POST https://openrouter.ai/api/v1/systemone`、model `jev-1.13` → 解析为 `typesafe/jev-1.13-20260917`、provider `TypeSafe`、
typed `answers.<id>.{noul|choice|score}`、不生成文本。与 `typesafe/jev-router` 属同一平台不同接口/上游。
**影响**：Jev 接入路径的后续选择（恢复 typed 端点 / 等 `jev-router` 成熟 / 换 provider）需新的授权；可用性探测见提案 §1.8。

## D-018 热路径 Jev 的真实目标是 typed `/api/v1/systemone`（NATIVE_TYPED_JEV_AVAILABLE）

**日期**：2026-09-28
**状态**：生效（人类授权的 P1/P2 探测后的事实认定）
**事实证据**（详见 `proposals/P0001.4.1-...md` §1.8）：
- P1 `GET /api/v1/models`（458 个模型）：`typesafe/jev-router` 已列出并被标记为 **Router**（`tokenizer=Router`、`pricing=-1/-1`、`supported_parameters=[]`、context 1e6）；
  `typesafe/jev-1.13` 与 `typesafe/jev-latest` 均**不在**该清单。
- P2 `POST /api/v1/systemone`（单条最小 `noul`，134 字节）：HTTP 200、604 ms、
  `model=typesafe/jev-1.13-20260917`、`provider=TypeSafe`、`answers.ok={type:noul,noul:0.99}`、`cost=1.1634e-05`、无 `confidence`。
**判定**：`NATIVE_TYPED_JEV_AVAILABLE`。
**含义**：Probex 热路径 Jev 的真实目标应是 `/api/v1/systemone`，而不是 chat completions 的 `typesafe/jev-router`。
**边界**：本条只是事实认定，**尚未**修改任何代码或 schema；实现 typed SystemOne provider（含 question schema 与 Choice/Noul 映射）需要新的提案与授权。
**不做**：不 fallback 到 chat completions、不修改 `jev-market-v1`、不增加兼容 parser、不做 prompt engineering。

## D-019 热路径 Jev Provider 采用 native typed System One（Chat Completions 路径正式废弃）

**日期**：2026-09-28
**状态**：生效（P0001.4.2 完成；人类批准实施）
**决策**：
- 热路径：`SystemOneProvider` → `POST https://openrouter.ai/api/v1/systemone`，请求 model alias `jev-1.13`，
  `{model, state, questions}`；resolved model / provider / response id / usage 写入 `PredictionRecord`。
- 五分类未来收益用 **Choice**（`market_5s|15s|30s|60s`）；adverse selection 与 fill 用 **Noul**（不用 Score）。
- `provider_confidence` 只取最近 horizon Choice 的 `confidence`；Noul 协议上无 confidence，**永不伪造**。
- wire ⇄ domain 适配：typed answers → `jev-market-v1` domain answers → **既有 strict parser** →
  `Prediction` 不变；`MarketState` / Scheduler / domain schema 语义均未改动。
- Chat Completions 热路径（`prediction.providers.openrouter`）**正式废弃**：保留模块作为 P0001.4.1 实验记录，
  但从包命名空间移除导出并标记 `DEPRECATED`（结构性测试保证不会被误用）。
**实测支撑**：409/462/413 ms（transport）、resolved `typesafe/jev-1.13-20260917`、provider `TypeSafe`、
cost ≈1.1e-04/call、五分类求和 = 1、Noul 无 confidence（详见提案 §1.6）。
**待人类/设计决定的业务参数**：adverse-selection 阈值 X（bps，必填构造参数，无默认值）；五分类是否需要数值分档。
**影响**：`PredictionRecord` 追加 `requested_model` / `resolved_model` / `response_id` / `usage`（均有默认值，向后兼容）。

## D-020 Accounting / Risk 核心的固定契约

**日期**：2026-09-28
**状态**：生效（P0001.5 完成）
**决策**：
- `Position.realized_pnl` **只含交易盈亏**；`trading_fees` / `funding` 独立累计；
  `net_realized = realized − fees + funding`（§7 与 §5 唯一自洽的读法）。
- `balance` 由账本派生（`initial + net_realized`）；`equity = balance + Σ unrealized`；两者严格区分。
- Fill 双键去重：`(venue, symbol, fill_id)` 与 `(venue, symbol, trade_id)`；重复只计数、不记账。
- 反手拆成 close leg + open leg（不混合均价）；`is_reversal` 显式记录。
- mark price 显式注入；未知 ≠ 0（缺 mark → unrealized / equity / notional / drawdown 为 `None`）。
- `RiskGate` 只消费 immutable `RiskSnapshot`；`portfolio.accounting` 只允许被 `risk/snapshot.py` 读取。
- reduce-only 真正降低暴露时只受硬检查约束（订单参数 / 数据完整性 / kill switch）；会增大暴露则拒绝。
- 已配置限额但数据缺失一律 REJECT；`available_balance = balance − open_order_exposure`；
  未配置 `max_leverage` 时可用余额检查按 1.0（不允许杠杆）。
- 时间边界（`now_ms` / `day_start_ts`）由调用方注入；`portfolio/**` 与 `risk/**` 不 import `time`/`datetime`。
**影响**：真实下单、order lifecycle、partial-fill 状态机、多币种换算、强平价推导均属后续阶段（P0001.6+）。

**人类裁决（2026-09-28，P0001.6 启动前）**：空仓无 mark 不允许新增仓位（保留 fail-closed）；
反手按 INCREASING（保留，Policy 可自行拆「先平后开」，Risk 不猜意图）；kill switch 拆三态（在 P0001.6 实现）。

## D-021 Execution 层契约（订单生命周期 / Paper 执行）

**日期**：2026-09-28
**状态**：生效（P0001.6 完成）
**决策**：
- `Order` 是不可变快照，`OrderStatus` 九态；终态（FILLED / CANCELED / FAILED / EXPIRED）不可回退，
  `LOST` 是「本地不确定」而非事实终态，只能由 reconciliation 恢复。
- **cancel request ≠ cancel success**：只有 `OrderCanceled` 进 CANCELED；PENDING_CANCEL 期间成交合法。
- **late fill**：终态之后、成交时间 ≤ 终态时间的 fill 必须记账（终态不变，更新 `final_executed_quantity`）；
  超出窗口或超量一律拒绝（fail closed）。
- **双层去重**：execution 层 `(symbol, execution_id)` / `(symbol, trade_id)` + accounting 的 `FillLedger`。
- **cancel-before-replace**：旧单必须确认终态才允许下新单（`OrderManager.replace`）。
- **pending exposure**：active 订单未成交部分按**订单自身价格**计入 `open_order_exposure`（最坏情形），
  由 `ExecutionEngine.snapshot()` 注入 RiskSnapshot（P0001.5 的 `open_order_exposure` 输入）。
- **记账路径唯一**：`ExecutionEvent → OrderTracker → canonical Fill → AccountingCore`；
  `ExecutionEngine` 是唯一接触 Accounting 的执行层组件。
- **kill switch 三态**：`NORMAL` / `REDUCE_ONLY`（只放行真正降暴露）/ `HALT_ALL`（禁 submit、**允许 cancel**）；
  P0001.5 的 `kill_switch: bool` 兼容为 HALT_ALL。
- **adapter 接口第一版同步**：`ExecutionAdapter.submit/cancel/poll/open_orders/recent_fills`。
  真实 Binance adapter（P0001.9）将引入异步 / user-stream 桥接，属新契约决策，不改 tracker 语义。
- **restart/recovery 只做逻辑恢复**：load local orders + 外部 open orders/fills → `reconcile`；
  `converged` 表示无需纠正动作（补记 fill 不算纠正）。
**影响**：盘口撮合（P0001.8）、Maker 报价（P0001.7）、真实交易所 reconciliation 与持久化（P0001.9+）均未实现。

## D-022 不确定订单暴露：LOST 与「资料不足」都必须继续占用风险额度

**日期**：2026-09-28
**状态**：生效（P0001.6.1 完成；取代 D-021 中「LOST 不计入 pending exposure」的取舍）
**决策**：
- Exposure 拆三视：`confirmed_open_exposure`（ACTIVE）+ `uncertain_exposure`（LOST）= `total_pending_exposure`；
  `RiskSnapshot.open_order_exposure` 使用 `total_pending_exposure`。
- `OrderTracker.open_order_exposure()` 保留为 `total_pending_exposure()` 的兼容别名。
- 资料不足、无法量化暴露的订单（例如 adopt 缺 side/quantity/price）**不按 0 处理**：记录为 `UnresolvedOrder`，
  `unresolved_order_count > 0` 时 RiskGate 拒绝**新增暴露**（`UNCERTAIN_EXPOSURE_UNKNOWN`），放行 reduce-only。
- 释放条件：只有 reconciliation 明确确认（CANCELED / FILLED / EXPIRED、恢复成功或资料补齐）才释放；
  仅「外部不再列出」不释放。
- 快照入参必须自洽（`total == confirmed + uncertain`），否则 `ValueError`（fail closed）。
**影响**：LOST 期间新暴露会被更保守地挡住 —— 这是刻意的 fail-closed 取舍（宁可少开仓，不可漏算风险）。

## D-023 Maker 策略层的契约（P0001.7）

**日期**：2026-09-28
**状态**：生效

**决策**：

- 新增 `strategy/` 领域层，只产出决策（`OrderProposal`，永远 `post_only=True`）与 KEEP/CANCEL/REPLACE/PLACE/NONE 分类；
  撤单与重挂仍由 `execution.OrderManager` 执行（cancel-before-replace 不变）。策略层不写 Accounting、不联网、不读 wall-clock
  （时间只来自 `RiskSnapshot.now_ms`）。
- 策略层只依赖 `execution.types`（订单事实契约），禁止依赖 `execution.engine/manager/adapters/tracker` 与 `prediction.runtime`；
  execution 层继续禁止依赖 strategy（双向隔离由测试固定）。
- 全部经济参数（tick、step、base_size、`minimum_edge_bps`、各阈值、目标仓位、因子上界、生命周期阈值）为 `MakerPolicyConfig`
  必填项、无默认值（§12）。
- `minimum_edge_bps` 的语义 =「新增暴露的报价相对 mid 的方向性优势下限」；由 tick 数解析求解，达到上限仍不满足 → 该侧不报价。
  reduce-only 报价豁免 cost floor（只降低风险）。
- Prediction 只通过「后退 tick 数 / 哪一侧允许报价 / 有界 confidence factor」影响决策；
  `buy/sell_fill_probability` **永不进入数量公式**（高 fill probability 可能正是高 adverse selection）。
- 全局门（HALT_ALL > 市场不可用 > prediction stale > 未知暴露 > REDUCE_ONLY）禁止新增暴露，且**只保留已有 reduce-only 挂单**；
  REDUCE_ONLY 模式例外地允许新挂 reduce-only。
- 无 prediction 时新增暴露一律禁止（cost floor 不可评估 → fail closed），这是推导结果而非新增业务规则。

**影响**：策略层可独立测试（114 条新测试），风险语义仍由 RiskGate 单点负责；P0001.8 的 event-level fill simulation 可在不改动本层契约的前提下接入。

**人类裁决补记（2026-09-28）**：

- 保留：`REDUCE_ONLY` 允许新挂 reduce-only；库存偏多时买侧「缩量 + 后退」而非停报（以后再加明确仓位阈值）。
- **否决**「无/过期 prediction 时不允许新增 reduce-only」。理由：持仓遇到 Jev 故障/prediction 过期时，若没有存量退出挂单，
  系统将无法主动降低风险，与「降低 exposure 应优先允许」的原则冲突。
  因此本决策中「全局门只保留已有 reduce-only、不新增」的部分**被修正为**：
  prediction 不可用时禁止新增**增加暴露**的报价，但**必须允许新增 reduce-only** 报价。
- 该修正需要子阶段（独立提案）落地；在落地前 P0001.7 保持「实现中」，`currentProposal = "P0001.7"`。
- 待明确项（不得由实现方自行决定）：`MARKET_UNHEALTHY` / `UNKNOWN_EXPOSURE` / `HALT_ALL` 下是否也允许新增 reduce-only；
  无 prediction 时 reduce-only 报价的定价与规模依据。

## D-024 Prediction 不可用时的降险连续性（P0001.7.1）

**日期**：2026-09-28
**状态**：生效（修正 D-023 中「全局门一律不新增 reduce-only」的部分）

**决策**：

- prediction 缺失 / 过期 / 缺 horizon 时：**只**禁止新增**增加暴露**的报价；**允许**新增 reduce-only 报价。
  理由（人类裁决）：已持仓 + Jev 故障 + 无存量退出挂单时，系统必须仍能主动降低风险（「降低 exposure 应优先允许」）。
- prediction 不可用时**完全退出方向性调整**：不沿用过期分布、不做 adverse-selection 后退/禁止、不做预测方向后退（不伪造 prediction）。
- 无 prediction 的 reduce-only 报价仍必须通过：市场可用性（tradeable / book / best bid+ask+mid）、KillSwitch、订单合法性、post-only、RiskGate。
  定价只用 best bid/ask + tick + 库存偏置；规模用有界 `confidence_factor` 下界。
- 生命周期：prediction valid → stale 时，增加暴露的旧报价 CANCEL；合法 reduce-only 报价走正常比较（KEEP 或 REPLACE），不是全部撤掉。
- 其他全局门保持不变：`MARKET_UNHEALTHY` / `UNKNOWN_EXPOSURE` / `HALT_ALL` 阻断全部新增报价；`REDUCE_ONLY` 熔断允许新增 reduce-only。
- `fresh_prediction()` 是「prediction 是否可用于方向性判断」的唯一判据。

**影响**：MakerPolicy 的 `PREDICTION_STALE` 分支 `allow_new_reducing=True`；观察项（`UNKNOWN_EXPOSURE` 与 `adverse_selection_block`
对 reduce-only 的处理、中断期的 replace churn）记录在提案 P0001.7 §2，需人类裁决后才能变更。

## D-025 Event-level Fill Simulation 的成交证据模型（P0001.8）

**日期**：2026-09-28
**状态**：生效

**决策**：

- **aggressor trade 是唯一成交证据**：`TradePayload`（`aggregate_trade_id` / `price` / `quantity` / `aggressor`）
  是市场事件词表中 `EventType.TRADE` 的载荷（P0001.1 早已保留该取值）。**L2 quantity 下降不是成交证据**
  （可能来自 cancel / modify / hidden liquidity / feed artifact），既不推进队列也不产生成交。
- **队列近似**：订单生效时 `queue_ahead = 该价位可见数量`；之后只由对手方向 aggressor trade 推进
  （先扣队列，剩余才是我们的成交）。`QueueState.UNKNOWN`（不可观察 / 盘口不可信 / gap 后未重建）时
  `queue_ahead is None`，**不得**产生推测性成交。
- **成交规则**：`trade price == limit` → `QUEUE_CONSUMED`；穿过限价 → `TRADE_THROUGH`（队列视为清空，
  量 = `min(剩余, aggressor 量)`）。成交价一律用订单限价；流动性一律 maker；手续费来自显式 `FeeSchedule`。
- **时间语义**：模拟时钟只由市场事件的 `process_ts` 推进（无 wall clock）；`submit_latency` 决定进入时刻、
  `cancel_latency` 决定撤单生效时刻（同刻撤单优先）；Replay 顺序 = Event Store recorded ordinal。
- **盘口不可信即挂起**：`BookHealth != HEALTHY` → `FILL_INFERENCE_SUSPENDED` + 队列作废；恢复后必须重建
  （`queue_rebuild_count` 留痕）或保持 `UNKNOWN`，绝不无声续算。
- **范围纪律**：`PaperBroker` 保持原职责（可控单元 / 故障测试）；`MarketBook` / Feature Engine / `market-state-v1` /
  Strategy / Prediction / Risk / Accounting **未改动**；真实 `aggTrade` 归一化与 `TradeFeatures` 接线留待后续阶段。

**影响**：Backtest 与真实 Live 之间的成交推断层落地，且每条模拟成交都带
`fill_reason` / `queue_state` / `event_ordinal` / `aggregate_trade_id` 证据，便于日后区分「高可信模拟」与「信息不足」。

## D-026 Live 公网行情接入的契约与端点（P0001.9.1）

**日期**：2026-09-28
**状态**：生效

**决策**：

- **零依赖手写 WS 传输**：握手 / 帧 / 掩码 / ping-pong / close / 分片全部用标准库实现（`transport.py`）；
  不使用 asyncio，由 `pump_once()` 同步驱动（确定性、可离线测试）。wall-clock 只允许出现在
  `transport.py` / `snapshot.py` / `runtime.py`（由 `tests/unit/test_live_isolation.py` 固定）。
- **端点单一 Owner**：`connectors/binance/market_data/endpoints.py` 是唯一硬编码 Binance URL 的地方；
  2026-03-06 公告的 tier 分层（`/public` = depth、`/market` = aggTrade/markPrice、combined `/stream?streams=`）
  按最佳可得证据固定，**未经官方文档核实**，因此全部可被配置覆盖，且**不做静默 fallback**。
- **aggressor trade 是唯一成交证据**（延续 D-025）：`aggTrade.m`（isBuyerMaker=true）⇒ aggressor = SELL；
  单调水位去重；trade 事件不进 FeatureEngine。
- **mark price 是独立事实**：`MarkPriceObservation` 不进 `MarketEvent`、不改 `market-state-v1`；本阶段只提供输入链与 telemetry。
- **TradingRules 只信 `filters`**：`pricePrecision` / `quantityPrecision` 永不作为规则来源；缺 filter 即报错。
- **传输层丢失必须让盘口失效**：新增 `MarketBook.invalidate(reason)` / `FeatureEngine.invalidate(reason)`
  （HEALTHY → STALE，本身是既有合法转换；非 HEALTHY 时幂等）+ 只读 `FeatureEngine.book_health`。
  这是 SC-9 的最小必要机制：传输层无法伪造 gap 事件，但重连后必须重新完成 snapshot + 增量对齐才恢复可信。
- **malformed 与单次快照失败不杀死 runtime**：计入 telemetry 并继续；重连用尽显式抛 `ReconnectExhaustedError`。

**影响**：Live 行情只读链路可离线全量验证（stub WS 服务端 + 注入式传输/HTTP）；真实公网 smoke 为 opt-in
（`PROBEX_LIVE_SMOKE=1`，无需凭据），端点的最终确认依赖该次运行。

## D-027 真实公网验收发现：`/public` diff 深度流跳号（待裁决）

**日期**：2026-09-28
**状态**：**待人类裁决**（不擅自改变 P0001.1 的完整性语义）

**已验证事实（真实 Binance USDⓈ-M，经人类本机 Clash 代理隧道，未改端点/未关证书校验）**：

- 端点与凭据：`https://fapi.binance.com`（depth / exchangeInfo / time）与
  `wss://fstream.binance.com/{public,market}/stream?streams=…` 全部可用；**无需任何 API Key**（SC-12 PASS）。
- 归属诊断（只读 15s、无 REST）：收到时间与交易所事件时间之差稳定在 −224…−140 ms ⇒ 链路新鲜，问题不在链路。
- `/public` 的 diff 深度流**跳号**：`@depth` 32 条消息中 12 条有 U/u 空洞（缺 13–315 个 id）；
  `@depth@100ms` 40 条中 35 条有空洞；`@depth@500ms` 21 条中 3 条；`@depth@250ms` **无数据**（非法后缀）；
  depth 在 `/market` tier **无数据**（tier 归属确认）。
- 后果：P0001.1「任何空洞 ⇒ STALE ⇒ 重同步」持续触发 ⇒ HEALTHY 时间占比 ≈14%，REST 重同步 ≈1 次/秒；
  且同步 REST 抓取阻塞读循环 ⇒ 观测到 7–36s 的 event lag 尖峰。
- 已实施的无关选项修复：`pump_once(max_messages)` 有界排空、`resync_cooldown_ms`（抑制 74 次、避免 REST 429）、
  depth 速度后缀可配置（默认 `@depth`）。

**待裁决选项**：A 改用部分深度快照流（`@depth20@100ms`，需明确 `sequence_contiguous` 语义）；
B 有界空洞视为可接受陈旧（弱化完整性，需显式授权）；C 保持保守策略（研究可用、Maker 不可用）；
D 非阻塞快照抓取（仅缓解新鲜度，可与 A/B/C 组合）。

**影响**：P0001.9.1 保持「实现中」，`currentProposal` 保持 `P0001.9.1`，未提交；在裁决前不改变任何完整性语义。
## D-028 修正：Futures diff depth 的连续性判据必须是 `pu == prev.u`（诊断 P0001.9.1.1）

**日期**：2026-09-28
**状态**：事实已核实（**修复待提案授权**）；**部分推翻 D-027**

**真实样本（经人类本机代理隧道，仓库外 `/tmp/probex_live/sample_*.jsonl`）**：

| 规则 | `btcusdt@depth`（178 对） | `btcusdt@depth@100ms`（294 对） |
| --- | --- | --- |
| `pu` 存在率 | 179/179 | 295/295 |
| `current.pu == previous.u` | **178/178（100%）** | **294/294（100%）** |
| `U <= prev.u+1 <= u`（D-003 现行窗口规则） | **0/178** | **0/294** |
| `U == prev.u + 1` | 0/178 | 0/294 |
| `pu - prev.u` | 恒 0 | 恒 0 |
| `U - prev.u`（median） | 116 | 120 |
| 单事件覆盖 id 数（median） | 27299 | 9972 |

**结论**：

- Futures diff 事件是**大范围聚合**的（覆盖上万 update id），`U` 与上一条 `u` 之间必然有间距 —
  **D-003 的窗口判据对 Futures 无效**（0% 通过），而 `pu` 判据 100% 通过且 `pu - prev.u` 恒为 0 ⇒ 数据无丢失。
- 因此 D-027 中「Binance 跳号 / 需要 A/B/C/D 选项」的判断**作废**：真实根因是连续性判据。
- 待授权修复（会修改 D-003 与 `BookDeltaPayload` 契约，需落盘提案）：
  解析并携带 `pu`（`BookDeltaPayload.previous_update_id: int | None`），
  `OrderBook` 优先用 `previous_update_id == last_applied_u`，无 `pu` 时回退窗口规则（现货）。
## D-029 Futures diff depth 连续性：锚点 + pu 语义（P0001.9.1.1 实施）

**日期**：2026-09-28
**状态**：生效（**细化 D-003 在 Futures 上的适用性**；现货语义不变）

**决策**：

- `BookDeltaPayload.previous_update_id`（Binance Futures `pu`）成为市场事件契约的一部分；
  `connectors/binance/market_data/depth.py` 解析它，Event Store codec 原样保存（旧记录解码为 None）。
- `OrderBook` 的连续性判据分两段：
  1. **快照锚点（刚应用快照后的第一条增量）**：vendor 文档的「跨过锚点」条件 ——
     Futures（有 `pu`）要求 `pu <= L < u`；无 `pu` 的 venue 用 `U <= L+1 <= u`（D-003 不变）。
     理由：REST 快照的 `lastUpdateId` 不是「上一条推送」，`pu` 在该刻不可比。
  2. **锚点之后**：有 `pu` 时以 venue 语义为准 —— `pu == 已应用的最后 update id` 即连续；
     `pu != last` 才是真实丢失（GAP → STALE → RESYNC）。没有 `pu` 的增量始终走窗口规则。
- 因此 **Futures 的 `U != last+1` 不再是异常**（一条事件聚合上万个 update id 是正常形态），
  这**不是**弱化完整性，而是修正 venue-specific sequence semantics（`Book decrease`/空洞语义不变）。
- `ALREADY_APPLIED`（`u <= last_applied`）仍在判定最前，与 `pu` 无关。

**证据（真实流，见提案 P0001.9.1.1 §1.1/§1.2）**：`pu` 100% 连续（473 对样本）、旧规则 0% 通过；
修复后真实 smoke **gap 0 / resync 1（初始）/ HEALTHY 100% / event lag 中位 −48 ms**（修前 7267 ms）。
## D-030 私有账户层（只读）的契约（P0001.9.2）

**日期**：2026-09-28
**状态**：生效（**未关闭**：真实凭据 smoke 未执行，SC-4/6/12/13 待补真实数据）

**决策**：

- **凭据纪律**：只从环境变量 `BINANCE_API_KEY` / `BINANCE_API_SECRET` 读取（唯一读取点 `auth.py`）；
  凭据与**签名**都按敏感处理——`repr`/`str`/异常/telemetry/事件记录一律遮蔽，
  REST 错误只报 `method + path + HTTP 状态`（签名等价可重放凭证）。
- **签名**：`HMAC-SHA256(secret, canonical_query)`，参数排序后追加 `timestamp` 与 `recvWindow`；
  发出的 query 与签名的 query 逐字节一致；签名时间戳 = 本地时间 + `ServerTimeOffset`（先测 `/fapi/v1/time`）。
- **只读边界**：本层没有任何下单/撤单/杠杆/保证金模式/持仓模式端点（静态测试固定）；
  只产出 `AccountSnapshotObservation` / `PositionObservation` / `OrderUpdateObservation` 等事实，
  不写 `OrderTracker` / Accounting、不驱动 `ExecutionEngine`；对账属 P0001.9.3。
- **账户模式**：只接受 one-way + USDT-M；`positionSide != BOTH` 或 `dualSidePosition=True` ⇒
  `UnsupportedAccountModeError`（fail closed，不自动兼容）。
- **启动顺序**：`listenKey → user stream → account/position snapshot → snapshot boundary`，
  避免「先快照、后开流」的状态空窗；边界（两个接收时间戳 + listenKey 状态）留给 P0001.9.3。
- **listenKey 生命周期**：7 态显式状态机（`STOPPED/STARTING/ACTIVE/RENEWING/EXPIRED/RECONNECTING/FAILED`），
  非法转换抛错；keepalive 间隔与 TTL 全部注入（TTL 官方 60 分钟，常量仅作参考）；
  续期失败 ⇒ `FAILED`；TTL 到期或 `listenKeyExpired` ⇒ 重建 + 重连。
- **连续性**：重连 / 重建 listenKey 后 `continuity_assumed = False`，只有新的 snapshot boundary 才恢复；
  不静默假设事件连续（真正对账留给 P0001.9.3）。
- **去重/乱序**：订单事件按每单 `cumulative_fill_quantity` 水位 + `(client_order_id, trade_id)`；
  账户事件按 `(transaction_ts, event_ts)` 水位；两者计数进 telemetry。
- **lag 语义**：`private_lag_ms` = 业务事件的 `receive_ts - event_ts` 分布（median/p95/max）；
  窗口内无业务事件时以 `snapshot_round_trip_ms`（私有 REST 路径 RTT）作为**明确标注**的替代基准；
  SC-13 的 BLOCKED 判定按 median 与 `max_median_private_lag_ms` 比较。
