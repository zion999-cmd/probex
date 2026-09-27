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
