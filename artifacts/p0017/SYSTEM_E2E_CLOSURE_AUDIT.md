# 系统终态闭环审计（System End-to-End Closure Audit）

**日期**：2026-10-12
**性质**：只读。未改代码/状态/提案；`currentProposal=null`。
**目的**：不再按"模块阶段"推进，而以**最终可运行系统**为约束，一次性建立总矩阵：每个目标运行路径的实现/集成/验证状态、缺口根因、以及一个**自身可验证闭合**的收口计划（完成后没有新的"连接层"缺口）。
**证据口径**：本报告只引用代码位置与已提交验收；外部网络（Binance 451）/凭据/写授权为环境前置，明确标 NOT RUN。

---

## 0. 最终目标运行模式（边界）

| 模式 | 数据 | 执行 | 是否最终目标 |
|---|---|---|---|
| **L0 离线回测（单/多标的）** | event-store JSONL | SimulatedVenue | 已交付（P0017.3/.4） |
| **L1 实时行情观察（多标的，不交易）** | Binance 公开 WS/REST | 无 | 代码已交付，**仅差真实网络 E2E（451）** |
| **L2 实时行情驱动模拟交易（多标的）** | Binance 公开 | 多个 SimulatedVenue + 共享组合风控 | **唯一真实的"连接层"缺口（G-1）** |
| **L3 Testnet 真实订单** | Testnet 公开+私有 | BinanceExecutionAdapter | 底座大部分在；P0001.16 Incomplete + 未接组合预算（G-2/G-3） |
| **L4 LIVE 实盘** | 主网 | 真实 | **冻结，不在当前授权范围** |

**关键结论先说**：从当前代码到"可运行系统"，真正缺的**新实现只有 1 个**——L2 的实时多标的执行编排（G-1）。它不是新领域组件，而是把两个已存在且接口已对齐的东西连起来（见 §3）。L3 是"补验收+接线+清残留"，不是新建。L1 只差网络。没有第四、第五个隐藏连接层。

---

## 1. 七层总矩阵（实现 / 集成 / 验证）

图例：✅完成且有测试 ｜ 🔗组件有、**接线缺** ｜ 🧪代码有、**真实验证缺（外部前置）** ｜ ❌未实现

| 层 | L0 离线回测 | L1 实时观察 | L2 实时模拟交易 | L3 Testnet |
|---|---|---|---|---|
| **数据面** | ✅ ReplaySource / market-dataset | ✅ MultiSymbolMarketCollector（发现/订阅/隔离/落盘/重连） | 🔗 同 L1，需把 `pump_once()` 输出接给执行编排 | 🧪 公开+私有流（`runtime/testnet.py`） |
| **决策面** | ✅ 单/多 FeatureEngine + MakerPolicy | ✅ FeatureEngine（MarketState） | 🔗 多标的决策（回测已有 `_decide_multi`，实时复用） | 🧪 engine.snapshot/decide |
| **执行面** | ✅ SimulatedVenue（单/多 adapter） | —（不交易） | 🔗 多 SimulatedVenue 已在回测验证；**缺实时 orchestrator 驱动** | 🧪 BinanceExecutionAdapter（submit/cancel/query/bridge） |
| **组合风险** | ✅ P0017.2 gross 硬闸（单+多回测） | — | 🔗 同一引擎即可（接线随 G-1） | ❌ **testnet 引擎未传 portfolio_commitment_provider**（G-3） |
| **会计面** | ✅ AccountingCore/FillLedger（单+多勾稽） | — | 🔗 同一 AccountingCore（随 G-1） | 🧪 真实成交已入过账（0.001 BTC 证据） |
| **运行治理** | ✅ 确定性步进/双跑/因果前缀 | 🧪 重连/恢复机制在（`ReconnectExhausted`/resume/gap resync），**长跑未真实验证** | 🔴 **G-1 必须定义**：实时事件→决策→成交的同步顺序、单 symbol 故障隔离、关闭语义 | 🧪 部分成交/撤单/异常恢复 SC 未达成（G-2） |
| **验收面** | ✅ 2541 测试 + CLI + manifest/report | 🧪 离线全绿；真实 smoke 待 451 解除 | ❌ **无端到端验收**（G-1 交付物） | 🧪 隔离链路真实成交 1 次；其余 NOT RUN |

---

## 2. 每个缺口的根因分类（缺实现 / 缺接线 / 缺测试 / 缺外部 / 缺授权）

| ID | 缺口 | 根因 | 所属层 | 是否阻塞"可运行系统" |
|---|---|---|---|---|
| **G-1** | 实时多标的行情 → 多标的决策/模拟执行/组合风控/会计 的**持续运行编排** | **缺接线 + 缺测试**（组件齐：collector `pump_once` 与回测消费同一 `MarketEvent`/`MarketState`；多 SimulatedVenue、共享 engine、`_decide_multi` 都在 backtest 验证过） | L2 | **是（核心）** |
| **G-1a** | L2 的运行治理：关闭时未成交挂单处理、单 symbol collector 故障隔离、背压 | 缺实现（小，随 G-1 的 orchestrator 生命周期） | L2 运行治理 | 是（G-1 内） |
| **G-2** | Testnet SC-11/12/13/6/7/15：限价挂单+撤单、部分成交、异常恢复的真实验收 | **缺外部（Testnet 授权+凭据+出口）+缺测试**；代码路径在 | L3 | 否（独立授权阶段） |
| **G-2a** | 测试网残留 `position=0.001 BTC`（open orders=0）阻塞 flat 前置 | 缺外部（需授权平仓或人工平） | L3 | 否 |
| **G-3** | Testnet ExecutionEngine 未接 P0017.2 组合预算（`runtime/testnet.py:795`） | **缺接线**（一行级 provider 注入 + 复用 P0017.2） | L3 组合风险 | 否（随 L3 阶段） |
| **G-4** | L1 真实多标的持续 smoke（连接/排序/完整性/隔离/重连/落盘） | **缺外部（451）**；离线机制全有测试 | L1 | 否（出口具备即可，无代码） |
| **G-5** | 3+ 标的专项（C-3）、per-symbol 全套 maker 参数（C-4） | 缺测试/缺配置 | 增强 | 否（C 类） |
| **G-6** | bar/OHLCV、slippage、funding、taker、真实队列校准、参数优化 | 明确未实现（设计排除） | 增强 | 否（不在最小可运行目标） |

**根因结论**：没有"无限连接层"。缺口收敛为：
- **1 个必做的接线+测试（G-1，含 G-1a）** → 解锁 L2（实时多标的模拟交易，无需真实下单）；
- **1 个外部验证（G-4）** → 解锁 L1 的"真实已验证"标记，零产品代码；
- **1 个独立授权阶段（G-2/G-2a/G-3）** → 解锁 L3，本质是补验收+少量接线+清残留；
- 其余是 C 类增强或明确排除项。

---

## 3. 为什么 G-1 是"接线"而不是又一个新系统（接口对齐证据）

| 回测侧（已验证） | 实时侧（已存在） | 对齐点 |
|---|---|---|
| `run_multi_backtest` 消费 `MarketEvent`（trade/book/mark） | `MultiSymbolMarketCollector.pump_once()` 产出 `MarketDataBatch.market_events: tuple[MarketEvent,...]` + `states: tuple[MarketState,...]` | **同一类型** |
| per-symbol `FeatureEngine.on_market_event` | collector 内部 runtime 已产出 `MarketState`（可直接用 batch.states，免重复算） | 同 `MarketState` |
| `MultiSymbolSimulatedAdapter` → per-symbol SimulatedVenue | 只需在 orchestrator 持有同一 adapter | 已实现 |
| 共享 `ExecutionEngine`（RiskGate+P0017.2+AccountingCore） | 回测已用 | 直接复用 |
| `_decide_multi`（per-symbol MakerPolicy/预算/提交） | orchestrator 每标的调用 | 已实现（在 `backtest/multi_driver.py`，应抽为共享步进函数，避免复制） |
| 时间 | 回测锚 exchange_ts；实时用 wall/事件时间 | **唯一语义点**：实时无"未来"，决策在 batch 到达后；需明确"一个 batch 内先喂状态再决策再 poll" |

**最小新增**（实现细节，待 P0017.5 设计冻结）：
1. 一个 `LiveMultiSymbolRunner`（或让 assembly 的 `binance-universe` 模式可选挂执行）：循环 `collector.pump_once()` → 对每个 symbol 按 `(exchange_ts)` 把 states/events 喂入对应 FeatureEngine/SimulatedVenue → 调共享 engine 的 per-symbol 决策（复用从 multi_driver 抽出的 step 函数）→ `engine.poll`；
2. 把 `backtest/multi_driver._decide_multi`/步进抽成共享纯函数（离线/实时共用，**消除"实时另写一套"风险**）；
3. orchestrator 生命周期：SIGTERM 优雅停止、不虚构收盘价、单 symbol collector error 不杀死全局（collector 已有 per-symbol try/except）；
4. 不引入任何真实下单（adapter 固定为 MultiSymbolSimulatedAdapter）。

**离线可测**：collector 可用注入式 http/ws factory（已有 `_universe_http_factory`/`transport_factory` 注入缝）做确定性 pump 测试，所以 **G-1 不依赖真实网络即可完成与验收**；真实长跑仅作为 G-4 的外部 smoke。

---

## 4. Testnet（L3）总账（不塞进最小目标，但一次列清）

P0001.16 的真实状态（来自其 Stop Condition 与 stop 报告）：
- **已真实验证**：下单/成交隔离链路真实跑通（venue orderId、0.001 BTC 真实成交、availableBalance 4998.73→4912.49、FillLedger/AccountingCore 入账、submit 路径 user-stream 桥接）。
- **未达成 SC**：SC-11 限价挂单+撤单、SC-12 maker 自然成交（故按人类裁决第 7 条不得 CLOSED）、SC-13 最终 flat、SC-6/7 stream 生命周期、SC-15 private E2E。
- **阻塞**：残留 0.001 BTC 使 `account_flat` readiness 前置失败（bootstrap 被挡）；`tests/acceptance/testnet_resume.py` 在跑（360 分钟有界），等真实私有事件。
- **接线缺口 G-3**：testnet engine 没有 portfolio provider（一行注入 + P0017.2 复用）。
- **前置**：人类单独授权 Testnet 写 + 凭据 + 可访问 demo.* 出口。

**判定**：L3 是一个边界清晰的独立阶段（建议命名时沿用 P0001.16 收尾，而非新开主线），交付物=清残留、G-3 接线、补 SC 真实证据、最终 flat。它**不是** L2 的前置（L2 用模拟器，不需要 Testnet）。

---

## 5. 收口计划（依赖排序，且每步有"完成即终止"的判据）

> 设计目标：执行完下列工作包后，**不再存在连接层缺口**；剩余只有"换出口跑一次真实 smoke"和"是否要 Testnet/实盘"的人工决策。

### WP-1（核心，建议作为下一个且唯一的产品提案 P0017.5）：L2 实时多标的模拟执行
- 范围：G-1 + G-1a。抽共享步进函数 + LiveMultiSymbolRunner + assembly/CLI 接线（`--market-source binance-universe --simulate`，或新 mode）。
- **完成判据（全部离线可测，不依赖真实网络）**：
  1. 注入式 collector（脚本化 batch）驱动 ≥2 标的，产生真实模拟决策/订单/成交/会计；
  2. P0017.2 组合预算在实时路径跨标的生效（含同 batch 预算竞争确定性）；
  3. 单 symbol collector 抛错不影响另一 symbol 与全局；
  4. 优雅停止：不强平、不虚构收盘价；进程退出无悬挂；
  5. 离线/实时共用同一步进函数（结构性测试：无重复决策/执行实现）；
  6. 不 import runtime.testnet/connectors 写路径（AST 守卫）；
  7. 全量回归 + detached。
- 完成后：**"实时行情→决策→组合风控→模拟成交→会计→可观察"端到端代码闭环**。

### WP-2（零产品代码，纯外部验证）：L1 真实 smoke = G-4
- 前置：Binance 出口（解除 451）。
- 动作：跑 `binance-universe` 观察 BTC/ETH，记录连接/排序/完整性/隔离/重连/落盘真实证据。
- 完成判据：真实事件持续入库、health、重连恢复、market-dataset 可查；P0017.1 的 §7-B NOT RUN 转 PASS。**没有新代码缺口**。

### WP-3（独立授权）：L3 Testnet 收尾 = G-2 + G-2a + G-3
- 前置：人类授权 Testnet 写 + 凭据 + 出口。
- 动作：清理/授权平掉 0.001 BTC、注入组合预算 provider、补 SC-11/12/13/6/7/15 真实证据、最终 flat、P0001.16 CLOSED。
- 完成判据：P0001.16 全部适用 SC 有真实证据且 flat。

### WP-4（C 类，可并行/延后，不阻塞可运行系统）：G-5/G-6
- 3+ 标的、per-symbol maker 参数；bar/slippage/funding 等。明确不是最小目标。

**WP-1+WP-2 完成后**，系统在"真实公开行情 + 模拟执行"意义上**端到端可运行且已验证**，且没有已知连接断点；L3/L4 是明确的、需要额外授权的独立风险决策，而不是"又发现一个缺口"。

---

## 6. 全系统最终验收条件（一次定义，替代"每阶段测试通过即完成"）

一个模式只有同时满足下列全部才可标记"可运行并已验证"：
1. **功能完整性**：该模式链路上无 🔗（接线断点）；L2 的判据见 WP-1。
2. **正确性**：风险/订单/成交/会计/因果有可执行测试（离线确定性）。
3. **运行可靠性**：断线/重连/重复/乱序/单标的故障/停止有明确行为，至少离线注入测试 +（实时模式）一次真实 smoke。
4. **端到端验收**：存在从输入到报告/状态的可重复自动化链路；外部环境部分单列实测证据，NOT RUN ≠ PASS。
5. **能力声明一致**：代码存在 ≠ 运行成功；模拟 ≠ 真实；L1≠L2≠L3≠L4。
6. **遗留闭合**：每个剩余项有根因（实现/接线/测试/外部/授权）、责任工作包、完成判据；凡最终目标必需者不允许长期挂 B/C。

---

## 7. 结论（直接回答"能不能真正做完"）

- **能，而且缺口已经闭合可数**：必做的新实现只有 **G-1（WP-1）一个工作包**，它是接线+抽取共享步进+测试，不是新领域系统；其全部依赖（实时 collector、多 SimulatedVenue、共享组合风控、多标的决策、会计）都已存在且在 P0017.1–.4 中验证，且 collector 输出类型与回测消费类型天然对齐。
- **不需要真实下单即可把"实时行情驱动模拟交易"做完并离线验收**（collector 有注入缝）。
- 真实公开行情验证（G-4）是"换网络出口跑一次"，零代码；Testnet（WP-3）是带授权的独立阶段，底座已 80%+ 在；实盘继续冻结。
- **建议**：批准把下一个（也是最后一个核心产品工作包）立为 **P0017.5 = WP-1（L2 实时多标的模拟执行）**；WP-2 等出口、WP-3 等 Testnet 授权。在 P0017.5 完成前，不新增其他模块型提案。

**本审计为只读，未做任何代码/状态变更。**
