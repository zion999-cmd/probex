# P0017.2 最终验收证据报告

**日期**：2026-10-11
**基线 commit**：`1cf6664`（已 push；detached clean checkout 复核）
**测试结果**：**2513 passed / 0 failed / 24 skipped**（detached clean checkout 一致）
**报告性质**：逐项对照设计文档 §13，区分「自动化已验证 / 仅静态或代码路径推断 / 真实运行未验证」。
**未改动**：本次仅新增本报告；未改代码、未改生命周期状态（`currentProposal=P0017.2` 保持）、未启动 P0017.3。

证据等级图例：
- **[T]** 由自动化测试确定性验证（无网络）
- **[S]** 仅静态代码路径/调用链核实（无对应断言测试）
- **[X]** 真实运行路径未验证（需外部条件/交易所）

---

## 0. 你点名的 5 个边界——核实结论

### 0.1 assembly 真实装配是否启用组合预算 / 禁用行为
- **启用**：`runtime/assembly.py:894` 读取配置 `risk.max_portfolio_gross_notional`；`:907` 把 `portfolio_commitment_provider` 注入 `ExecutionEngine`；`:1576 _portfolio_commitment_provider` 从 accounting 持仓 + `tracker.exposure_by_symbol()` 构造 `PortfolioCommitment`。**[S]**（装配路径存在；无独立的 assembly 级断言，但有引擎级端到端 [T] 覆盖同构造逻辑）
- **禁用（两层，确定）**：
  1. 配置缺省 ⇒ `_portfolio_commitment_provider` 返回 `None`（`runtime/assembly.py:1579`）**[S]**；
  2. 引擎侧 provider 为 None ⇒ `_evaluate_portfolio_budget` 直接返回 None，组合闸不运行（`execution/engine.py`）**[T]** `test_disabled_when_limit_none`、以及 2513 全量绿证明 `max_portfolio_gross_notional=None` 时既有单标的链路零回归。
- 结论：组合预算**默认关闭、显式配置才启用**，禁用时行为与变更前一致。

### 0.2 runtime/testnet.py 独立授权路径——明确不在范围
- **事实**：`runtime/testnet.py:795` 构造自己的 `ExecutionEngine(...)`，**没有**传 `portfolio_commitment_provider`（`grep -c = 0`）⇒ 即使配置了组合限额，Testnet 引擎的组合闸也不生效。**[S]**
- 风险边界：
  - **不得把 PAPER 的"单决策线程、无 TOCTOU"结论外推到 Testnet/LIVE**。Testnet 走独立的 `build_testnet_product_service`、readiness authority、user stream 与提交路径；P0017.2 未对其增加任何组合预算保证。
  - Testnet 的单标的 `RiskGate` 行为**不变**（同一 gate、同一 reason 语义），只是没有"跨标的合计"这道第二闸。
  - 若未来要在真实路径启用组合预算，需要：显式传入 commitment provider（需多标的真实持仓/挂单事实）+ 独立提案与写授权——本提案未授予。
- 零交易所写入：本次改动无任何提交/撮合/账户写接口变更。**[T]**（守卫与全量测试）

### 0.3 LOST / unresolved / 缺失 mark / 成交-会计一致性
- **LOST**：`exposure_by_symbol()` 把 LOST 计入 `uncertain`（`execution/tracker.py`）**[T]** `test_lost_counts_as_uncertain`；组合层 `has_unresolved` ⇒ `PORTFOLIO_EXPOSURE_UNKNOWN` fail-closed **[T]** `test_unresolved_orders_fail_closed`。
- **unresolved**：`UnresolvedOrder` 新增可选 `symbol`；`note_unresolved_order` 自动从已知 order 补 symbol，未知则 None；`unresolved_count_for_symbol` 对 symbol=None 的保守计入每个标的（不遗漏）**[S]**；既有 15 个 fault 测试（`tests/fault/test_uncertain_exposure.py`）全绿，证明 per-symbol 作用域修正未破坏不确定暴露语义 **[T]**。
- **缺失 mark（一个需要你知道的细节）**：组合 commitment provider 在 `pos.notional` 为 None（缺 mark 的非 flat 持仓）时**跳过该持仓**（`runtime/assembly.py` `if pos.notional`）。单看组合投影可能少计；但 `engine.snapshot(symbol)` 对同一 accounting 取 mark，**单标的 RiskGate 会以 `MISSING_MARK_PRICE` 拒绝该 symbol 的新增暴露**（`risk/gate.py:202`）**[T]**（`test_risk_gate` 42 项）。因此在"两道闸都在"的 PAPER 路径上，缺 mark 的标的无法获批新单——纵深防御成立，但**组合投影本身不 fail-closed**，这是一个应在未来收紧的点（见 §3 已知限制），本次按最小改动未改 accounting。
- **成交→会计一致性（D6 互斥前提）**：`ExecutionEngine.on_events/poll` 在同一同步调用内先 `manager.on_events`（tracker 更新 filled/remaining），再 `_account(updates)`（`accounting.record_fill`），下一次 submit 看到的就是一致视图 **[S]**（调用链核实）；部分成交 `remaining=quantity−filled`，已成交只进持仓、剩余只进在途 **[T]** `test_confirmed_exposure_is_remaining_notional` + 引擎端 `test_..._pending_order_consumes_portfolio_budget_before_fill`。

### 0.4 两道闸执行顺序 / reason code / 候选单登记
- **顺序**：`engine.submit` 先 `_evaluate_portfolio_budget`，通过后才 `snapshot(symbol)` + `gate.evaluate`（单标的）（`execution/engine.py:147-170`）**[T]**。
- **候选单登记**：组合闸拒绝时直接返回 `ExecutionResult(rejection=...)`，发生在 `manager.submit` **之前** ⇒ tracker 不登记、不产生订单、不占预算 **[T]** `test_second_symbol_blocked...` 断言 `ETHUSDT not in tracker.orders`。
- **reason code 区分**：组合超限 `PORTFOLIO_NOTIONAL_LIMIT`；不可量化 `PORTFOLIO_EXPOSURE_UNKNOWN`；单标的仍用既有 `NOTIONAL_LIMIT` 等，二者在 trace/UI 经 catalog 区分 **[T]** `test_..._reason_code`；catalog 中英文解释已加（`product/reason_catalog.py`）**[S]**。
- **无重复消费窗口**：候选单通过后立即在同一 submit 内 `manager.submit` 登记为 ACTIVE（计入下次在途）；决策为单线程顺序，下一个决策才可能读到它 **[T]** `test_pending_order_consumes_portfolio_budget_before_fill`（未成交在途 6000 + 第二单 6000 被拦）。

### 0.5 24 项 skipped 是否覆盖本次变更路径
- 全部 24 项 skipped 都是 **opt-in 真实外部测试**（`-v` 实测）：
  - `tests/live/test_binance_*`：需 `PROBEX_LIVE_SMOKE=1` / `PROBEX_LIVE_PRIVATE=1`（真实 Binance 网络/凭据）；
  - `tests/live/test_systemone_live` 等：需 `JEV_LIVE_TEST=1` + `OPENROUTER_API_KEY`（真实 SystemOne）。
- **没有任何一项**对应 portfolio budget / exposure_by_symbol / 组合闸——这些全部是离线确定性单测/集成测，均实际执行并通过。**[T]**
- 含义：skipped 不削弱本次变更的离线证据；但真实 Binance/Testnet 路径的组合预算**未被任何测试覆盖**（设计如此，见 0.2）。

---

## 1. §13.1 组合预算测试逐项

| 设计要求 | 结果 | 证据 |
|---|---|---|
| 限额 None 时单标的行为不变 | **[T] PASS** | `test_portfolio_budget.py::test_disabled_when_limit_none` + 全量 2513 零回归 |
| 各标的过、组合超限则组合闸拒（6000+6000>10000） | **[T] PASS** | `test_two_symbols_each_ok_but_portfolio_over_limit_rejected`（纯函数）、`test_portfolio_gate_engine.test_second_symbol_blocked...`（真实引擎） |
| projected == limit 允许 | **[T] PASS** | `test_equal_to_limit_is_allowed`（代码用 `> limit+1e-9`，等值放行） |
| 单标的自身超限仍由旧闸拒绝 | **[T] PASS** | 既有 `test_risk_gate.py` 42 项 + 引擎测试中单标的 `max_position_notional=8000` 配置；组合闸先过、单标的闸独立判定 |
| gross 不多空抵消 | **[T] PASS** | `test_gross_does_not_net_longs_and_shorts`（+6000 多/4000 空，再 1 名义即拒） |
| 缺价格/无效输入不当零 | **[T] PASS（单标的闸）/ [S]（组合投影，见 0.3、§3）** | `test_unresolved_orders_fail_closed`；缺 mark 由 `MISSING_MARK_PRICE` 兜底 |
| 组合/单标的 reason code 区分 | **[T] PASS** | `PORTFOLIO_NOTIONAL_LIMIT` vs `NOTIONAL_LIMIT` 断言 |
| unresolved 无法量化 ⇒ fail-closed | **[T] PASS** | `test_unresolved_orders_fail_closed` → `PORTFOLIO_EXPOSURE_UNKNOWN` |

## 2. §13.2 订单状态与会计逐项

| 设计要求 | 结果 | 证据 |
|---|---|---|
| 部分成交已成交部分只入会计一次 | **[T] PASS** | 既有 `test_paper_execution.py`（overfill/重复）+ fill 双键去重；本次未改记账 |
| 部分成交只按剩余量计在途 | **[T] PASS** | `test_confirmed_exposure_is_remaining_notional`（2@100 成交 0.5 ⇒ 在途 150） |
| 撤单中继续占预算 | **[T] PASS** | PENDING_CANCEL ∈ `_ACTIVE_STATUSES`（`execution/types.py:72`），`active()` 仍纳入；既有状态机测试 |
| 权威终态不保留剩余敞口 | **[T] PASS** | `test_terminal_orders_contribute_nothing`（FILLED/CANCELED → 空） |
| LOST/未知不静默释放 | **[T] PASS** | `test_lost_counts_as_uncertain` |
| 迟到/重复成交不重复记账/计算 | **[T] PASS（既有）** | `test_cancel_then_late_fill_keeps_terminal_status`、双键去重测试；本次未触碰该路径 |
| 反手覆盖平仓+反向开仓 | **[T] PASS** | `test_oversized_reduce_only_counts_full_notional`（超量 reduce-only 按全额 6000，不放行） |
| reduce-only 超量 + 与其他挂单共存 | **[T] / [S]** | 超量保守 [T]；"与其他在途共存"由 gross 求和天然覆盖（在途全计入），但**无专门的多单共存断言** [S] |
| 候选单只计一次 | **[T] PASS** | 组合闸在 manager.submit 前评估；拒绝不登记、通过才登记一次；无预占/回滚双计数路径 |
| 已接受订单预算在下次决策前可见 | **[T] PASS** | `test_pending_order_consumes_portfolio_budget_before_fill` |
| per-symbol 在途与组合总量同口径守恒 | **[T] PASS** | `test_conservation_sum_equals_total_pending`（Σ by_symbol == total_pending_exposure） |

## 3. §13.3 并发与回归逐项

| 设计要求 | 结果 | 证据 |
|---|---|---|
| 两个顺序决策不能共同超额 | **[T] PASS** | 引擎端到端测试（持仓与在途两种形态） |
| 证明单决策线程假设，或暴露同步缺口 | **[S] 已证明（PAPER）/ [X]（Testnet 不外推）** | PAPER 唯一新增暴露入口 `RuntimeDecisionLoop`（单线程单 asyncio loop，同步 `engine.submit`）；`runtime/testnet.py` 是另一路径，未在本提案范围 |
| 限额关闭时单标的回归通过 | **[T] PASS** | 2513 全量 |
| RiskGate 硬检查顺序/symbol mismatch/reason 语义完整 | **[T] PASS** | `test_risk_gate.py`、`SYMBOL_MISMATCH` 守卫仍在（`gate.py:163`），全量绿 |
| MakerPolicy 不因本次改变 | **[T] PASS** | `strategy/**` 零改动（`git diff` 可核）；`risk/budget.py` 未改（注：MakerPolicy 的 `remaining_exposure_budget` 暂未纳入新组合约束——见 §4） |
| 组合预算模块自身无交易所写操作 | **[T] PASS** | `risk/portfolio_budget.py` 纯函数、零 import 连接器/账户；架构守卫全绿 |
| 确定性、不依赖实时网络 | **[T] PASS** | 新增 18 测试全部离线确定性 |

---

## 4. 已知限制 / 未覆盖（诚实记录，不扩大结论）

1. **[S→应收紧] 组合投影对缺 mark 持仓是"跳过"而非 fail-closed**（`runtime/assembly.py` `if pos.notional`）。当前靠单标的 `MISSING_MARK_PRICE` 纵深防御，PAPER 安全；但严格按设计 §12，组合层最好在"有非 flat 持仓却无法取 mark"时直接返回不可建立上界。**建议作为后续小改，不在本次最小改动内。**
2. **[S] MakerPolicy 的事前报价预算 `risk/budget.py::remaining_exposure_budget` 尚未纳入 `max_portfolio_gross_notional`**：组合闸在 submit 时强制（事后不会超限），但策略**报价规模**暂不会因组合限额而缩小。这意味着策略可能继续报价、在 submit 被组合闸拒（安全但可能产生更多 reject）。设计 §7.1/预算报告提到"MakerPolicy 自动据此缩减"，本次只完成了硬闸，**未改 `risk/budget.py`**（避免改策略行为，符合非目标"不修改 MakerPolicy"）。是否要让预算函数感知组合限额，需要你决策（会触及策略 sizing，建议单列）。
3. **[X] 真实多标的 PAPER 端到端**：组合预算的端到端测试用真实 `AccountingCore/OrderTracker/ExecutionEngine` + PaperBroker（离线确定性），但**不是**多 symbol 真实行情驱动的长时段运行；P0017.1 的真实行情 E2E 本身仍 NOT RUN（451）。
4. **[X] Testnet/LIVE 组合预算**：`runtime/testnet.py` 未接线组合闸（0.2），无真实/测试覆盖；这是独立授权事项。
5. **[S] reduce-only 与"其他在途单共存"** 缺专门断言（逻辑由 gross 全计入覆盖）。
6. **真实成交的多空 net 口径**：系统明确选 gross，未实现也不应把 net 当预算；`net_exposure` 仅用于展示，不参与本闸。

## 5. 结论

- 设计 §13 中**可在离线确定性环境验证的项，全部由自动化测试 PASS（[T]）**；核心安全主张（跨标的合计约束、在途成交前占预算、候选不重复登记、gross 不抵消、unresolved/LOST fail-closed、守恒、禁用零回归）都有真实引擎级或纯函数断言。
- **[S] 项**是调用链/配置核实（装配启用/禁用、submit 顺序、成交-会计同步时序、Testnet 不接线、skipped 构成），证据已列文件:行号。
- **[X] 项**明确依赖真实交易所网络/凭据或独立授权，**未运行、不记为 PASS**：真实多标的行情 E2E（受 451）、Testnet/LIVE 组合预算、SystemOne 相关 8 项。
- 核心 Owner（AccountingCore/OrderTracker 唯一所有权、RiskGate 单标的职责、FillLedger 去重、MakerPolicy 逻辑、CAPITAL unavailable、零新写路径）均未改变。

**是否将 P0017.2 正式收口、`currentProposal` 置 null，由人类据本报告决定；在此之前保持 `P0017.2`。**
