# P0017.3 只读差距审计（Gap Audit）

> **归档状态（2026-10-12）**：这是 P0017.3 **实施前**的只读差距审计；其结论已被后续实现采纳（缺失项→`backtest/` 包），并由验收报告与 Closure Audit 收口。作为可追溯证据保留；其中“真实缺失”状态以文首日期为准，不代表最终状态。

**日期**：2026-10-11
**性质**：只读。未改代码/配置/状态；未创建/提交/删除文件；`currentProposal=null`（P0017.2 已收口）。
**依据设计**：`proposals/P0017.3-offline-backtest-driver-evaluation-report.md`。
**后续证据**：同目录 `P0017_3_ACCEPTANCE_REPORT.md`、`P0017_3_CLOSURE_AUDIT.md`。
**证据口径**：[T] 有测试/代码可直接核实；[S] 代码路径推断；[M] 当时真实缺失（需新建，后已实现）。

---

## 0. 路径与 Git 状态不一致（你的第 1 点）

- 实际文件：`proposals/P0017.3-offline-backtest-driver-evaluation-report.md`（**未跟踪**，git status `??`）。
- 文档自称：`Target document: artifacts/p0017/P0017_3_DESIGN.md`，但该文件**不存在**（`ls artifacts/p0017/` 无 P0017_3_*）。
- 建议（待你决定，我不擅自移动/提交）：
  1. 保留现状（设计正文在 `proposals/`，实施记录放 `artifacts/p0017/`，把文档内 `Target document` 改为"实施记录将建于此"）；或
  2. 你授权后我把它转为正式命名 `proposals/P0017.3-offline-backtest.md` 并纳入版本控制。
- 本审计输出：`artifacts/p0017/P0017_3_GAP_AUDIT.md`（本文件，是否提交等你指示）。

---

## 1. 底层能力盘点（对照设计 §4 的接口映射）

| 设计要求的边界 | 现有实现（文件:符号） | 状态 |
|---|---|---|
| 事件级模拟成交 | `execution/simulation/venue.py::SimulatedVenue`（submit/cancel/poll/on_market_event，队列+费+延迟+no-future，仅 maker/post-only） | **[T] 完整** |
| 费/延迟模型 | `simulation/fees.py::FeeSchedule(maker_fee_rate)`、`simulation/latency.py::LatencyModel(submit_ms/cancel_ms)`；**无 taker、无 slippage 字段** | [T] maker 有 / **[M] taker/slippage 无** |
| 历史事件源 | `market/replay/source.py::ReplaySource(JsonlEventReader)`（FULL/STEP，ReplayClock，reset）；event store JSONL | **[T] 完整（事件存储）** |
| 多标的共享数据集 | `storage/market_dataset.py`（P0017.1，record 形态非 `MarketEvent`） | [T] 有，但见 §3 适配 |
| 决策路径 | MakerPolicy 经 `RuntimeDecisionLoop`；可被历史事件驱动（PAPER 已证） | **[T]** |
| 单标的风险 | `RiskGate` + `build_risk_snapshot` | **[T]** |
| 组合风险（P0017.2） | `risk/portfolio_budget.py`（gross 第二道闸）+ engine `portfolio_commitment_provider` | **[T]** |
| 记账 | `AccountingCore`/`FillLedger`/`FundingLedger`（fees/funding/gross/net/equity） | **[T]** |
| 成交/订单事件回流 | `ExecutionEngine.poll/on_events → manager → accounting` | **[T]** |
| 评估指标 | `reports/metrics.py`（sharpe/run_mdd/profit_factor/resample；run-internal、非年化声明）+ `reports/builder.py` | **[T] 单 run 维度** |
| 报告 | `RunSummary` JSON + Markdown（确定性） | **[T]** |
| run 身份/配置指纹 | `RunRecord.config_fingerprint / data_range`（存在但当前多为 Fact.unknown 占位） | [S] 字段在，**[M] 内容哈希/manifest 机制缺** |

---

## 2. 差距矩阵：对照设计 §13 的 35 条强制 SC

图例：**[T] 已有可复用并可验证** ｜ **[R] 有底层但需接入/缺验证** ｜ **[M] 真实缺失，需新建（最小组件）**

### 13.1 离线隔离（SC 1–5）
| SC | 判定 | 依据/差距 |
|---|---|---|
| 1 无凭据/无网络可完成 | **[R]** | 组件全离线，但**没有独立回测入口/命令**；现在只能靠 `ProductRuntime(PAPER)+feed` 启动一个"长跑式"runtime |
| 2 证明未调用交易所写接口 | **[M]** | 需离线 harness 注入并断言 adapter 非真实写路径（回测 driver 用 SimulatedVenue，可加结构性断言） |
| 3 不初始化 Testnet/LIVE | **[M]** | 需一个明确的 offline driver，构造上不可达 `runtime/testnet.py` |
| 4 PAPER/Testnet 行为不变 | [T] | 新 driver 并列即可（守卫 + 全量） |
| 5 451 不阻塞离线验收 | [T] | 回测只读本地 JSONL，无外网 |

### 13.2 因果（SC 6–10）
| SC | 判定 | 依据/差距 |
|---|---|---|
| 6 决策看不到未来事件 | **[R]** | `SimulatedVenue` 有 `entry_ts/no-future`；但**回测 driver 的可见性边界尚未接线**（谁在何时把哪个事件给策略 vs 给 venue） |
| 7 新单不能由不合格的过去事件成交 | **[R]** | venue 按 `entry_ts` 之后事件成交（`venue.py:284`）；需回测保证订单 entry 与事件时序正确 |
| 8 bar 边界信号不能拿提交时不可得的同 bar 信息成交 | **[M]** | 无 bar 级回测执行模型；当前是**逐事件（trade/book）**，不是 OHLCV bar。设计 §6.3 允许 trade/quote 驱动；**bar-only 能力缺失**，最小路径应限定为事件驱动，bar 模式显式 UNSUPPORTED |
| 9 模糊 intrabar 保守/标 UNSUPPORTED | **[M]** | 无 bar 模型则无 intrabar 歧义；需在报告中声明执行粒度=事件级 |
| 10 改未来观测不改变其可用之前的决策/成交 | **[M]** | 需 look-ahead 注入测试（改尾部事件→前缀结果不变；可复现双跑） |

### 13.3 风险与会计（SC 11–17）
| SC | 判定 | 依据/差距 |
|---|---|---|
| 11 组合限额启用时跨标的超额被拒 | **[T]** | P0017.2 `test_..._portfolio_gate_engine`；回测 driver 必须注入 commitment provider（[R] 接线） |
| 12 gross 不抵消多空 | [T] | `test_gross_does_not_net...` |
| 13 在途/持仓不重复；部分成交转移 | **[T]** | tracker 守恒 + `remaining=qty-filled`；回测复用同一 manager/accounting |
| 14 单标的检查与 reason 语义保留 | [T] | gate 未改 |
| 15 缺 mark/unresolved 不变零 | **[T]** | L1 修正 + `PORTFOLIO_EXPOSURE_UNKNOWN` |
| 16 订单/成交/持仓/费/equity 守恒 | **[R]** | 会计已守恒（既有测试），但**需回测级 integrity check 汇总为 run 通过/失败条件** |
| 17 注入会计不一致 ⇒ INTEGRITY_FAILURE 而非成功报告 | **[M]** | 无回测结果状态机（设计 §11 六类 outcome 均无） |

### 13.4 成本与指标（SC 18–23）
| SC | 判定 | 依据/差距 |
|---|---|---|
| 18 已知费率⇒期望总成本 | **[R]** | maker fee 已算（`SimulatedFill.fee`）；缺回测级断言 |
| 19 已知滑点⇒期望成交价调整 | **[M]** | **无 slippage 模型/字段**；需"窄扩展"（设计 §7.1 允许 narrowly-scoped compatible extension） |
| 20 部分成交不漏费/不双收 | **[R]** | venue 按成交量计费；缺回测级断言（maker 已双收防护由 accounting 双键） |
| 21 gross/cost/net 守恒 | **[R]** | accounting 有 `realized/fees/net_realized`；报告需分列（当前 RunSummary metrics 未显式 gross-cost-net 三段） |
| 22 成本输入缺失⇒标缺失/UNSUPPORTED，不静默为 0 | **[M]** | FeeSchedule 已强制显式费率（好基础）；回测配置校验/报告标注需新建 |
| 23 不可得统计指标不伪造 | **[R]** | metrics 已有 Fact.unknown 语义；报告需逐指标可用性 |

### 13.5 可复现与报告（SC 24–30）
| SC | 判定 | 依据/差距 |
|---|---|---|
| 24 manifest 标识 code/data/config/成本模型 | **[M]** | `RunRecord.config_fingerprint` 占位为 unknown；**需 code commit + dataset 内容 hash + 完整配置 + seed 的不可变 manifest** |
| 25 同输入+seed 双跑等价 | **[M]** | 需确定性 driver（当前 PAPER 用 wall-clock + 线程，**不保证逐笔复现**）+ 双跑比对测试 |
| 26 改实质输入⇒run 身份改变 | **[M]** | 需 canonical input identity（hash） |
| 27 机读/人读报告指同一 run | **[M]** | 需 backtest result 产物（可复用 RunSummary 形态 + backtest 扩展段） |
| 28 失败/未完成/成功可区分 | **[M]** | 需设计 §11 的六类 outcome |
| 29 detached 可复现 fixture E2E | **[M]** | 需提交一个内容寻址的固定 fixture 数据集 + driver |
| 30 证据记录命令/结果/skipped | [T] 流程已有惯例 |

### 13.6 回归与范围（SC 31–35）
31–32 [T/M] 全量+新 skip 纪律；33 [T]（并列 driver 不改既有）；34 [M]（不引入写/优化器/新执行模型——driver 自身约束）；35 流程项。

---

## 3. 因果与会计审计（你的第 4 点）

1. **历史数据可见性**：现有 PAPER 回放里，`MarketFeedProvider` 把同一事件**同时**喂 FeatureEngine（决策输入）和 SimulatedVenue（成交）（`runtime/provider.py` market_event_sink）。回测必须显式建模"决策在 t 只能见 ≤t 的状态、订单 entry_ts 之后才可能成交"——`SimulatedVenue` 已保证成交侧 no-future（[T]），但**策略决策侧的快照时点**需要 driver 用历史时钟严格驱动，不能沿用 wall-clock 线程（[M]）。
2. **事件顺序**：venue 内确定性顺序 `entry_ts → client_order_id`（`venue.py:481`）；回测 driver 需实现设计 §5.3 的 9 步顺序（观测→state→决策→risk→submit→后续事件→fill→accounting→记录），现有 PAPER 是"feed 推事件 + 独立决策线程轮询"，**顺序模型不同**，回测应是**单线程事件步进**（类似 `ReplaySource.STEP` + 每步显式 decide），不能直接复用线程 loop。
3. **时间戳语义**：`exchange_ts/receive_ts/process_ts` 三时间戳齐备（[T]）；回测时钟应锚定 `exchange_ts`（历史），`receive/process` 在离线可等于 exchange 或显式建模延迟——需在 manifest 声明（[M]）。
4. **手续费/滑点**：仅 maker fee，无 slippage（[M]）。
5. **持仓/订单状态**：复用 tracker/accounting 即自动一致（[T]），关键是 driver 不绕过 `engine.submit/poll`。
6. **组合风险**：回测可直接复用 P0017.2，但需要 driver 提供 commitment（accounting+tracker 都在离线进程内，可行 [R]）。
7. **可复现**：当前 PAPER 闭环**不是确定性回测**（节流 sleep + 后台线程 + wall clock，见 e2e 用轮询等待）——这是与回测最大的本质差距，必须新建确定性步进 driver，而不是给 PAPER 加报告。

---

## 4. 最小实施路径（仅提议，不实现）

> 原则：复用 SimulatedVenue / MakerPolicy 决策路径 / RiskGate / portfolio gate / AccountingCore / metrics；**只新建回测编排、manifest、结果状态、必要的窄 slippage 扩展**；执行粒度限定**事件驱动**（bar-only 显式 UNSUPPORTED）。

建议批次（对应设计 §12，可在实施时再细化）：
- **B1 离线确定性 runner（核心，M 最大头）**：`backtest/` 新包，单线程：读固定 JSONL/数据集 → ReplayClock 步进 → 每事件更新 state → 在决策点同步调用既有 decision→risk(含组合)→engine.submit→SimulatedVenue→engine.poll→accounting；构造上不 import `runtime/testnet`/`live`，不触网。覆盖 SC 1/3/6/7/25/34。
- **B2 manifest 与 run 身份**：dataset 内容 hash + code 版本 + 完整配置（含 fee/latency/slippage/portfolio 是否启用）+ seed → canonical run id；不可变结果；改输入改 id。覆盖 SC 24/26。
- **B3 成本窄扩展 + 报告**：可选 slippage（显式、保守、可关）；结果分 gross/cost/net；指标可用性逐一声明；复用 metrics/RunSummary。覆盖 SC 18–23。
- **B4 结果状态 + integrity checks**：设计 §11 六类 outcome；订单/成交/持仓/fees/equity 守恒校验失败 ⇒ INTEGRITY_FAILURE。覆盖 SC 16/17/28。
- **B5 fixture E2E + 因果测试**：提交内容寻址固定数据集；双跑等价；look-ahead 注入（改未来不改过去）；无写接口断言；detached 复核。覆盖 SC 2/8/10/29 等。

**明确不在最小范围**：bar/OHLCV 回测与 intrabar 撮合（标 UNSUPPORTED）、taker 复杂模型、参数寻优、市场冲击/真实队列精度、funding（除非 fixture 含 funding 事件且语义支持）。

---

## 5. 待你决策的点（实施前）

1. **设计文件归属**：维持 `proposals/P0017.3-*.md` 未跟踪，还是授权我转正命名并提交（§0）？
2. **回测执行粒度**：同意最小路径限定为**事件驱动（trade/book）**、bar-only 显式标 UNSUPPORTED 吗？（这直接决定 SC 8/9 不需实现 bar 撮合）
3. **回测输入数据集**：第一版用 **event-store JSONL（ReplaySource 已支持）**作为 canonical 输入，P0017.1 的 market-dataset 作为可选输入（需加一层 MarketEvent 适配），可以吗？
4. **slippage**：是否在第一版就加一个**显式可关的固定 bps 滑点**窄扩展，还是第一版仅 maker fee、滑点标 UNSUPPORTED（更保守，我倾向后者起步）？
5. **入口形态**：新建 `cli`/`scripts` 子命令（如 `probex backtest --dataset ... --config ... --out ...`）离线产出报告，对吗？

**结论**：P0017.3 的可复用底座（决策/风险/组合闸/会计/事件模拟/指标）大部分已存在并有测试；**真实缺失集中在"确定性离线 runner + manifest/run 身份 + 结果状态/完整性校验 + 因果/look-ahead 测试"**，外加明确的执行粒度边界。不需要另起回测框架，但也不能把当前 wall-clock 的 PAPER 运行直接当回测。等你确认上述 5 点后，我再产出实施任务（仍不直接写实现代码）。
