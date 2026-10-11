# P0017.2 只读耦合分析：组合风险预算语义

**日期**：2026-10-11
**性质**：只读架构审计。**未改任何代码/配置/Persona/Prompt/状态文件**；未创建/批准 P0017.2；未触碰核心 Owner；零交易所写入。
**口径约定**：【事实】代码可直接核实（附文件:符号）；【推断】基于事实的架构推论；【待决策】必须由人类正式决定，不由实现发明。

---

## 0. 一句话结论

现有风险系统是**严格单标的（single-symbol-per-snapshot）**结构：`RiskSnapshot` 带一个 `symbol`，`RiskGate` 硬性拒绝 symbol 不一致（`SYMBOL_MISMATCH`）。但**账本（AccountingCore）天然是多标的**，且已提供两个现成的组合级聚合量（`gross_exposure` / `net_exposure`），日亏损/回撤/余额/equity 本身就是组合级。因此 P0017.2 **不需要发明新风险数学**，缺的是一个"组合级预算检查"层：在每标的既有 gate 之外，对跨标的合计再加一道约束。预算口径在代码中**事实上已经是 notional**（名义价值）——这是最可复用、最诚实的起点。

---

## 1. 四个核心组件的真实定义与调用链

### 1.1 RiskLimits（`risk/limits.py:31 RiskLimits`）【事实】
- 字段（全部 `None` = 未启用；配置了但事实缺失 ⇒ gate 一律 REJECT，fail-closed）：
  - 单标的持仓：`max_position_qty`、`max_position_notional`
  - **在途订单合计（已是"该 symbol 维度的合计"）**：`max_open_order_exposure`
  - **天然组合级（与 symbol 无关）**：`max_daily_loss`、`max_drawdown_pct`、`max_leverage`
  - 其他：`min_liquidation_distance_bps`、`max_mark_age_ms`、kill switch
- **结论**：`RiskLimits` **没有**显式的"跨 symbol 总持仓/总在途 notional 上限"字段。`max_open_order_exposure` 当前只在**单 symbol 快照**上判定，不是多 symbol 合计。

### 1.2 RiskSnapshot（`risk/types.py:214 RiskSnapshot` + `risk/snapshot.py build_risk_snapshot`）【事实】
- 强单标的：字段 `symbol: str`；`build_risk_snapshot(accounting, *, symbol, ...)`。
- 提供的风险数据：
  - 持仓：`position_qty`、`position_notional`（该 symbol，按 mark）
  - 组合级账本聚合（注意：这些**不是**该 symbol 的，而是整个账户的）：`balance`、`equity`、`gross_exposure`、`net_exposure`、`available_balance`、`realized_pnl_today`、`unrealized_pnl`、`drawdown/drawdown_pct/peak_equity`
  - 在途：`open_order_exposure`（= confirmed ACTIVE + uncertain LOST 未成交 notional，**按调用方传入**，当前 engine 传入的是**整个 tracker 的合计**，见 1.5）
- `gross_exposure`/`net_exposure` 来自 `AccountingCore`（`portfolio/accounting.py:316/327`）：`Σ|qty·mark|` 与 `|Σ signed qty·mark|`，**任一持仓缺 mark 即为 None（fail-closed）**。

### 1.3 RiskGate（`risk/gate.py RiskGate.evaluate`）【事实】
- 入参 `(OrderProposal, RiskSnapshot)`；第一步 `_check_order_parameters` 即断言 `proposal.symbol == snapshot.symbol`，否则 `SYMBOL_MISMATCH`（`risk/gate.py:163`）。**这是多标的最硬的结构性约束。**
- 对"增加暴露"（INCREASING / REVERSING 的开仓部分）依次检查（`_increasing_exposure_checks`）：
  1. unresolved/uncertain 暴露（有不可量化订单 ⇒ 全拒）
  2. **单标的持仓**：`max_position_qty`、`max_position_notional`（`projected_notional = |position_qty + signed_qty| * mark`）
  3. **在途 + 余额**：`max_open_order_exposure`（`snapshot.open_order_exposure + proposal.notional > limit`）；`available_balance * effective_leverage`
  4. **组合级损失**：`max_daily_loss`、`max_drawdown_pct`（本就是账户级）
  5. `max_leverage`（按账户 equity）、强平距离
- reduce-only 降暴露：只校验方向真正降低暴露，随后 `_allow_reduction`，**不施加**持仓/notional/损失限额（`risk/gate.py:132` 注释明确）。

### 1.4 AccountingCore（`portfolio/accounting.py`）【事实】
- 已多标的：`_positions: dict[str, Position]`、`_marks: dict[str,float]`；`position(symbol)`、`update_mark_price(symbol,...)`。
- 组合聚合已存在：`balance`、`equity()`、`gross_exposure`、`net_exposure`、`realized_pnl()`（跨 symbol 求和）、`trading_fees_since/funding_since`。
- 含义：**组合分母（equity/balance）与组合持仓名义（gross/net）都无需新造**。

### 1.5 OrderTracker / Manager / Engine（在途风险）【事实】
- `OrderTracker.confirmed_open_exposure()` = `Σ active.remaining·price`；`uncertain_exposure()` = `Σ lost.remaining·price`（`execution/tracker.py:191-203`），**对 tracker 内全部订单求和（不按 symbol 分桶）**，但产品当前是单标的运行，所以实际只有一个 symbol。
- `ExecutionEngine.snapshot(symbol, now_ms)`（`execution/engine.py:216`）调用 `build_risk_snapshot(..., open_order_exposure=self.manager.open_order_exposure(), ...)`，即把**整个 tracker 的在途合计**塞进**单 symbol 快照**。【推断】单标的下正确；多标的下若仍只建一个单 symbol 快照，会出现"把 B 的在途暴露算进 A 的快照"或"组合合计无处归属"的语义错位。
- `submit()` 紧邻提交做一次新鲜 gate（`execution/engine.py:143-146`）。

### 1.6 决策侧（`risk/budget.py remaining_exposure_budget`）【事实】
- `MakerPolicy` 通过 `remaining_risk_budget` 控制报价规模（`strategy/maker/policy.py` 多处）；该预算由 `remaining_exposure_budget(snapshot, limits)` 计算，取**已配置约束的最小剩余 notional**，语义与 gate 完全一致（只读暴露，不改规则）。同样是**单 snapshot/symbol**。

---

## 2. 单标的约束 vs 组合约束：现有缺口

【事实】当前可同时成立的约束层级：

| 层级 | 现有？ | 字段/来源 | 多标的是否正确 |
|---|---|---|---|
| 单标的持仓限额 | ✅ | `max_position_qty/notional`，per-snapshot | 需"每 symbol 一份快照" |
| 单标的在途限额 | ⚠️ 名字像、作用域错 | `max_open_order_exposure` 现对**全 tracker** 求和却放进单 symbol 快照 | 需明确为 per-symbol 或组合（见方案） |
| **组合总持仓/总在途 notional** | ❌ 缺 | 无对应 limit 字段；但 `gross_exposure` 数据已有 | **P0017.2 核心新增** |
| 组合日亏损 | ✅ | `max_daily_loss`（账户级） | 已天然组合 |
| 组合回撤 | ✅ | `max_drawdown_pct` + HWM | 已天然组合 |
| 杠杆/可用余额 | ✅ | equity/balance 账户级 | 已天然组合 |
| kill switch | ✅ | 账户级 | 已天然组合 |

【事实】你举的"两个 symbol 各批 6000、合计 12000 > 组合预算 10000"在**现有代码中不会被拦**：因为 `max_position_notional` 是在各自的单 symbol 快照上判定，没有任何一处对 `Σ projected gross notional` 做约束。`max_open_order_exposure` 因 tracker 求和的偶然实现，只有当两个 symbol 的订单进**同一个 tracker 且用同一个 limit 值**时才"碰巧"拦得住在途合计——但它没有覆盖已成交持仓，且作用域未被契约固定，**不能依赖这个偶然性**。

【待决策】组合约束要同时覆盖两类"已承诺预算"：
1. **已持仓**：Σ projected gross position notional（数据已有：gross_exposure）；
2. **在途新单**：所有 symbol 的 active+uncertain pending notional（tracker 已有合计能力，但需 per-symbol 可分）。
新增订单的组合占用 = 组合现有 gross（持仓+在途）+ 本单 notional。

---

## 3. 预算单位（Notional / Margin / Loss-at-risk）在代码中的真实使用

| 口径 | 代码现状 | 证据 |
|---|---|---|
| **Notional（名义价值）** | **风险限额与在途暴露的事实单位**。持仓限额 `max_position_notional`、在途 `open_order_exposure`、预算 `remaining_notional`、Order/Position `.notional` 全是 notional | `risk/limits.py`、`risk/budget.py ExposureBudget.remaining_notional`、`execution/types.py:213 Order.notional=remaining·price`、`portfolio/position.py:72 Position.notional=|qty|·mark` |
| **Margin（保证金）** | **风险预算不使用 margin**。仅 live 侧读取交易所 `availableBalance`/杠杆容量做"可用容量"检查（`available_balance * effective_leverage`），这是**容量上限**不是保证金占用核算；系统不自行计算初始/维持保证金 | `risk/gate.py` `_check_open_order_and_balance`；无 IM/MM 计算模块 |
| **Loss-at-risk** | **不存在**独立的潜在损失估算（无 VaR/压力情景/保证金风险）。最接近"损失"语义的是账户级 `max_daily_loss`（已实现净亏损阈值）与 `max_drawdown_pct`（峰值回撤阈值）——两者都是**已实现/已盯市结果**，不是前瞻损失预算 | `risk/limits.py`、`RiskSnapshot.realized_pnl_today/drawdown_pct` |

【推断】因此"组合风险预算"最诚实、可立即验证的口径是 **notional（名义风险占用）**，并继续用已有的**账户级 daily-loss / drawdown / leverage** 约束真正的"亏损风险"。把预算命名为 margin 或 loss-at-risk 都会**暗示系统具备它实际没有的计算语义**（保证金模型 / 前瞻损失分布），违反"不用未证实的风险公式补齐语义"。

【待决策】若未来要以"保证金占用"或"潜在损失"为预算单位，需要先正式立项做保证金/风险计量模型（交易所 tier、IM/MM、或显式 loss 规则），不应塞进 P0017.2。

---

## 4. 组合预算语义：两个方案（附依据/约束/一致性/问题）

> 两方案都**不改 RiskGate 的判定数学**，而是新增一个组合级判定，复用既有 gross/net/equity 事实；都保持 OrderTracker/AccountingCore/RiskGate 职责归属不变。

### 方案 A（推荐，最小改动）：单标的 Gate 保留 + 新增"组合 Gross 预算"第二道闸

**结构**
- 每个 symbol 继续产生**自己的** `RiskSnapshot`（`build_risk_snapshot(..., symbol=s)`），过**现有 `RiskGate`**（单标的持仓/在途 per-symbol、以及账户级日亏损/回撤/杠杆——这些账户级检查在每次 evaluate 都跑，天然覆盖组合）。
- 新增一个**组合投影**（建议放 `risk/`，如 `risk/portfolio_budget.py`），输入：
  - 每 symbol 的 `position_notional`（持仓）+ 每 symbol 的在途 notional（从 OrderTracker 按 symbol 分桶求和）+ 本次 proposal.notional；
  - 输出一个 `PortfolioRiskSnapshot`（或轻量 dataclass）：`committed_gross_notional = Σ_s(position_notional_s + pending_notional_s)`、`available_equity/balance`、各 symbol 明细。
- 新增**组合限额字段**（在 `RiskLimits` 上以可选项追加，**不改旧字段语义**）：
  - `max_portfolio_gross_notional`（总持仓+在途名义上限）；
  - 可选 `max_portfolio_open_order_notional`（只约束在途；若不设则并入 gross 项）。
- 判定顺序：**先组合闸、后单标的闸**（组合超限直接拒，避免逐 symbol 都过却合计超限）。
- `remaining_exposure_budget` 增加一个组合约束候选（与现有"取最小"机制一致），MakerPolicy 自动据此缩减每个 symbol 的报价，**无需改策略逻辑**。

**计算依据（全部现有）**：持仓 gross=`AccountingCore.gross_exposure`（或按 symbol `Position.notional` 求和）；在途=`OrderTracker` 按 symbol 的 `remaining·price`；新单=`proposal.notional`。

**约束规则**
- `committed_gross_notional_after = Σ existing per-symbol (position+pending) + proposal.notional ≤ max_portfolio_gross_notional`
- 任一 symbol 缺 mark / 有 unresolved 订单 ⇒ 组合投影不可计算 ⇒ **fail-closed REJECT**（与现有 unknown≠0 一致）。
- 单 symbol 维度同时满足 `max_position_notional`（per symbol）。
- reduce-only：当且仅当**降低该 symbol 暴露且不增加组合 gross**（反向反手 REVERSING 的开仓部分仍占用组合预算）时豁免组合新增检查——与现有 `classify_exposure` 语义对齐。

**状态一致性要求**
- 组合投影与 per-symbol 快照必须在**同一提交临界区/同一 now_ms** 构建（沿用 ExecutionEngine 紧邻 submit 的新鲜快照模式），避免两 symbol 并发各自通过、合计超限（TOCTOU）。当前 PAPER 是单决策线程（RuntimeDecisionLoop），多 symbol 在同一线程轮转即可满足；真正并发提交需要在引擎加组合检查锁（实施时定）。

**优点**：改动最小、风险数学零新增（全是 notional 求和与既有账户级约束）；单标的行为完全不回归（新 limit=None 时组合闸不生效）。
**问题/代价**：
- 需要把 `max_open_order_exposure` 的作用域**正式澄清**为 per-symbol（方案 A 下），并修正 engine 目前"全 tracker 合计塞进单 symbol 快照"的传参（改为按 symbol 过滤）——这是**语义修正**，需在提案中显式记录（旧行为只在单标的运行，故无历史破坏）。
- 需要 OrderTracker 增加 `exposure_by_symbol()`（只读聚合，**不改所有权**）。
- gross 口径对"多空对冲"较保守（BTC 多 + ETH 空也全额相加）；这是保守、可验证的选择。

### 方案 B：统一组合快照（PortfolioRiskSnapshot）驱动唯一评估

**结构**
- 不再为 gate 构造"每 symbol 一个单 symbol 快照"，而是构造一个**含全部 symbol 明细 + 组合聚合**的 `PortfolioRiskSnapshot`；RiskGate（或新增 `PortfolioRiskGate`，旧 gate 保留单标的用途）一次性评估某 proposal：先查该 symbol 的 per-symbol 限额，再查组合限额，再跑账户级损失/回撤/杠杆。
- 限额分两层配置：`per_symbol: dict[symbol, RiskLimits-ish]` + `portfolio: {max_gross_notional, ...}`。

**计算依据**：同方案 A 的现有字段，只是聚合一次完成。

**优点**：单一评估点，无 TOCTOU；per-symbol 与组合约束在同一对象内，审计更清晰；天然支持"每 symbol 不同限额"。
**问题/代价**：
- 改动面大：`build_risk_snapshot`/`RiskGate.evaluate`/`ExecutionEngine.snapshot`/`risk/budget.py` 都要扩展或新增并行版本，回归风险高（违背 §29 禁止大爆炸式重写）。
- 现有大量测试按单 symbol 快照编写，需要适配层才能不回归。
- 与现有"RiskGate 只消费 RiskSnapshot"的纪律需要一次正式契约升级（应作为 ADR/Decision 记录）。
- 多 symbol 差异化限额在 P0017.2 是否真需要，尚未被需求证实（可能 YAGNI）。

【推断】**建议方案 A**：它把"组合约束"做成第二道、可独立关闭（limit=None）、复用现有 notional 与账户级约束的闸，最小化对已验收单标的链路的扰动；方案 B 留待未来确有 per-symbol 差异化限额或高并发要求时再做。

---

## 5. 最小改动建议（不实施）

### 5.1 新增
- `risk/portfolio_budget.py`：`PortfolioRiskCommitment`（dataclass：committed/pending/by_symbol）+ 纯函数 `portfolio_gross_after(...)` + `evaluate_portfolio_limit(proposal, commitment, limits) -> RiskDecision | None`（复用 `RiskReasonCode`，可能需新增 1 个 code，如 `PORTFOLIO_NOTIONAL_LIMIT`——**新增 reason code 属契约扩展，需提案确认**）。
- `OrderTracker.exposure_by_symbol() -> dict[str, float]`（只读，按 `order.symbol` 聚合 confirmed+uncertain 的 `remaining·price`）；Owner 仍是 OrderTracker。
- `RiskLimits` 追加可选字段 `max_portfolio_gross_notional: float | None = None`（及可选在途字段）；`None` = 不启用，保证单标的零回归。
- `risk/budget.py`：把组合约束加入 `remaining_exposure_budget` 的候选最小值（limit 配置时）。
- 多标的 PAPER 编排（P0017.2 主体，非本审计）：每 symbol state/策略上下文，共享同一个 tracker/accounting/gate，在同一线程顺序 evaluate，组合闸在 per-symbol 闸之前。

### 5.2 需要澄清/修正（写入提案，不当实现暗改）
- 澄清 `max_open_order_exposure` 作用域为 **per-symbol**（方案 A）；修正 `ExecutionEngine.snapshot` 把全 tracker 合计传入单 symbol 快照的做法 → 改为按 symbol 过滤（`tracker.exposure_by_symbol().get(symbol, 0.0)`）。【事实：当前传全量；`execution/engine.py:226`】
- 明确组合预算口径 = **gross notional（持仓+在途）**，单位 USDT（计价币），不称为 margin/loss-at-risk。

### 5.3 明确不改
- RiskGate 既有判定数学、AccountingCore 记账、OrderTracker 状态所有权、FillLedger 双键去重、MakerPolicy 策略逻辑、CAPITAL/unavailable_by_design、任何写路径。

### 5.4 涉及文件（预估）
`risk/portfolio_budget.py`(新)、`risk/limits.py`、`risk/budget.py`、`risk/types.py`(reason code)、`execution/tracker.py`(只读聚合)、`execution/engine.py`(per-symbol 在途传参 + 组合闸调用点)、`product/`(组合风险只读投影)、`api/`+`ui/system`(展示组合预算占用)、`runtime/`(多标的 PAPER 编排)、相应测试。

### 5.5 测试方案（先写测试契约）
- **单元（纯函数，无网络）**
  - 两个 symbol 各 6000、组合限额 10000 ⇒ 第二单被 `PORTFOLIO_NOTIONAL_LIMIT` 拒；限额 12000 ⇒ 两单都过（你的例子固化为测试）。
  - 单 symbol 限额与组合限额同时存在：任一超限分别命中正确 reason code。
  - gross 口径：多/空都计入 gross；net 不用于预算（若决定用 net 另议）。
  - 任一 symbol 缺 mark / unresolved 订单 ⇒ fail-closed。
  - reduce-only 降暴露不被组合闸误拒；REVERSING 的开仓部分仍占预算。
  - `exposure_by_symbol` 与既有 `confirmed/uncertain` 合计一致（总量守恒：Σ by_symbol == total_pending）。
  - `max_portfolio_gross_notional=None` ⇒ 行为与现状逐字节一致（单标的回归）。
- **集成（多标的 PAPER，离线事件）**
  - 单决策线程内顺序提交多 symbol 订单，验证"逐 symbol 过、组合不过"被拦；不串标（FillLedger/Order.correlation）、PnL 不重复。
  - 组合预算占用随成交/撤单/成交回报正确增减；kill switch/daily-loss/drawdown 账户级约束跨 symbol 生效。
- **守卫**：新增架构断言——RiskGate/AccountingCore/OrderTracker 仍是单例 Owner；产品层不复制风险数学；无新第三方依赖。
- **真实 E2E**：本阶段为 PAPER，不需要交易所；真实多标的沿用 P0017.1 的 NOT RUN/451 口径。

---

## 6. 待人类决策清单（P0017.2 立项前必须回答）

1. **预算口径确认**：采用 **gross notional（持仓 + 在途，USDT 计价）作为组合预算单位**？（推断建议：是；明确不使用 margin/loss-at-risk）
2. **方案选择**：A（单标的闸 + 组合第二道闸，推荐）还是 B（统一组合快照）？
3. **新增限额字段命名/语义**：是否接受 `max_portfolio_gross_notional`（持仓+在途总额）；是否还要单独的 `max_portfolio_open_order_notional`（仅在途）？
4. **多空口径**：组合预算用 **gross（Σ绝对值，保守，推荐）** 还是 net（|Σ 带符号|，会净额对冲，可能低估最坏情形）？
5. **per-symbol 限额**：P0017.2 是否所有 symbol 共用同一组 `RiskLimits`，还是需要每 symbol 不同限额（方案 B 的动因；当前需求未证实，建议先共用）？
6. **reason code**：是否同意新增 `PORTFOLIO_NOTIONAL_LIMIT`（属 RiskReasonCode 契约扩展）？
7. **语义修正授权**：是否同意把 `max_open_order_exposure` 正式澄清为 per-symbol、并修正 engine 的在途传参（单标的历史无破坏，但属语义明确化）？
8. **并发模型**：多标的 PAPER 是否限定单决策线程顺序评估（满足一致性、零锁）？还是要求跨线程并发（需组合检查临界区，复杂度上升）？

**在以上 1–8 决策落地前，不应开始 P0017.2 实现。**
