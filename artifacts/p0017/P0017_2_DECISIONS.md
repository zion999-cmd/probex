# P0017.2 组合风险预算 — 立项前决策表（待人类逐项确认）

**日期**：2026-10-11
**状态**：**待决策**。本表不是提案、不是批准、不授权实现；逐项确认后才形成 P0017.2 正式设计。
**依据**：`artifacts/p0017/P0017_2_PORTFOLIO_RISK_ANALYSIS.md`（只读耦合分析）。
**图例**：`[ ]` 待确认；**建议**列为 Pi 基于现有代码的推荐，非决定。

---

## A. 你已给出倾向的 6 项（请确认或修改）

### D1. 预算口径
- **建议**：**Gross notional（组合总名义敞口），USDT 计价**。
- **代码依据**：持仓/在途/剩余预算的事实单位已是 notional（`Position.notional=|qty|·mark`、`Order.notional=remaining·price`、`ExposureBudget.remaining_notional`）；系统**无**保证金（IM/MM）计算、**无**前瞻 loss-at-risk。
- **固定语义**：gross notional **不是风险损失**；真正的亏损风险继续由账户级 `max_daily_loss` / `max_drawdown_pct` / `max_leverage` 约束。
- `[ ]` 确认 gross notional ／ `[ ]` 修改为：______

### D2. 架构方案
- **建议**：**方案 A——保留单标的 `RiskGate`，新增组合第二道闸**（`risk/portfolio_budget.py` 纯函数 + 可选组合限额）。
- **理由**：最小改动；组合限额默认 `None` ⇒ 单标的链路零回归；不重写 `build_risk_snapshot/RiskGate/ExecutionEngine`。
- `[ ]` 选 A ／ `[ ]` 选 B（统一组合快照，改动大）／ `[ ]` 其他：______

### D3. 组合统计方式
- **建议**：**Gross（Σ 绝对值），非 net**。
- **公式**：`G = Σ_{s∈S} |q_s · p_s|`（持仓部分），组合闸实际检查 projected gross（含在途，见 D6）。
- **理由**：`AccountingCore.gross_exposure` 已用同口径（任一缺 mark ⇒ None，fail-closed）；net 会让多空对冲掩盖总敞口。
- `[ ]` 确认 gross ／ `[ ]` net ／ `[ ]` 两者都暴露但预算只按 gross：______

### D4. 组合限额字段
- **建议**：在 `RiskLimits` 追加**一个**可选字段 `max_portfolio_gross_notional: float | None = None`（含义=持仓+在途的组合总名义上限）；默认 `None`=不启用。
- **暂不**加 `max_portfolio_open_order_notional`（仅在途）——YAGNI；如确有"只限制挂单不限制持仓"需求再加。
- `[ ]` 确认仅一个字段 ／ `[ ]` 需要再加在途专属字段 ／ `[ ]` 其他命名：______

### D5. 拒绝原因 code
- **建议**：新增枚举 `RiskReasonCode.PORTFOLIO_NOTIONAL_LIMIT = "PORTFOLIO_NOTIONAL_LIMIT"`，与现有单标的 `NOTIONAL_LIMIT`（`risk/types.py:72`）区分。
- **影响**：reason catalog（72 条）需加人类解释；trace/UI 自动经既有 `reasonCell` 展示。属 RiskReasonCode 契约扩展。
- `[ ]` 确认新增 ／ `[ ]` 复用现有 code ／ `[ ]` 其他命名：______

### D8. PAPER 并发模型
- **建议**：**单决策线程顺序评估**。多 symbol 在现有 `RuntimeDecisionLoop` 单线程内轮转（`runtime/decision_loop.py:166` 现为每 loop 一个线程），组合闸与 per-symbol 闸在**同一 now_ms 临界区**顺序执行 ⇒ 无 TOCTOU、无需新锁。
- **代价**：多标的报价是顺序而非并行；PAPER 足够。未来真正并发再引入组合检查临界区。
- `[ ]` 确认单线程顺序 ／ `[ ]` 要求并发（需额外锁设计）：______

---

## B. 你尚未明确、必须拍板的 3 项

### D6. 在途订单如何计入 projected gross（**最关键，防重复计数**）
- **建议统计范围**（单一 committed 视图，持仓与在途互斥不重叠）：
  - 持仓部分：每个 symbol 的 `Position.notional = |持仓量|·mark`（已成交）。
  - 在途部分：每个 symbol 的 **ACTIVE + LOST 订单剩余未成交** `Σ remaining·order.price`（与 `OrderTracker.total_pending_exposure` 同口径），按 symbol 分桶（新增只读 `OrderTracker.exposure_by_symbol()`）。
  - 本单：`+ proposal.notional`。
  - **projected portfolio gross = Σ_s |position_notional_s| + Σ_s pending_notional_s + proposal.notional**
- **边界固定**：
  - *部分成交*：已成交部分进持仓、`remaining` 部分进在途——同一数量不会两边出现（`Order.remaining_quantity`）。
  - *撤单中（PENDING_CANCEL）*：仍计入在途，直到 CANCELLED 终态确认（保守，预算不提前释放）。
  - *FILLED/CANCELLED/REJECTED 终态单*：不进在途（FILLED 已在持仓）。
  - *LOST 单*：继续占用在途（与现有 uncertain exposure 纪律一致）。
  - *反手（REVERSING）*：开仓部分的 notional 计入预算（与 `classify_exposure` 对开仓部分的处理一致）。
  - *reduce-only*：当且仅当真正降低该 symbol 敞口且不增加组合 gross 时，豁免组合新增检查（沿用 `classify_exposure.REDUCING`）。
- `[ ]` 确认此 committed 口径 ／ `[ ]` 在途按 mark 而非 order.price（口径变化，需说明）／ `[ ]` 其他：______

### D7. `max_open_order_exposure` 作用域正式澄清 + engine 传参修正
- **现状（事实）**：字段名像 per-symbol，但 `ExecutionEngine.snapshot()` 把**整个 tracker 的在途合计**塞进单 symbol 快照（`execution/engine.py:226`）；单标的运行下无差异，多标的下会把 B 的挂单算进 A 的检查。
- **建议**：
  1. 正式把 `max_open_order_exposure` 语义固定为 **per-symbol**（文档 + 字段含义）；
  2. 新增 `OrderTracker.exposure_by_symbol() -> dict[str, float]`（只读聚合，Owner 不变），engine 改为传 `exposure_by_symbol().get(symbol, 0.0)` 的 confirmed/uncertain 分量；
  3. **守恒测试**：`Σ_s exposure_by_symbol()[s] == tracker.total_pending_exposure()`（防聚合漏算/重算）。
- **回归面**：仅当存在多 symbol 订单时行为变化；当前产品单标的，故历史行为不破坏，但这是**语义正式化**，需要你明确授权。
- `[ ]` 授权澄清为 per-symbol 并修正传参 ／ `[ ]` 改为组合级（则与 D4 关系需重议）／ `[ ]` 其他：______

### D9. 多 symbol 是否共用同一组 RiskLimits
- **建议**：**P0017.2 所有 symbol 共用同一组 `RiskLimits`**（含相同的 per-symbol `max_position_notional` 与一个组合 `max_portfolio_gross_notional`）。
- **理由**：当前没有"每标的不同限额"的已证实需求（YAGNI）；差异化限额会把方案 A 推向方案 B 的复杂度。未来需要时再扩展 `dict[symbol, limits]`。
- `[ ]` 确认共用 ／ `[ ]` 需要 per-symbol 差异化（则倾向方案 B）：______

---

## C. 决策后才会发生的事（本次均不做）

- 形成 `proposals/P0017.2-*.md` 正式设计（含上面固定语义、逐文件影响、验收 SC）。
- 你批准 P0017.2 后：`currentProposal` 才从 P0017.1 切到 P0017.2 并开工。
- 不改 `RiskGate` 判定数学、`AccountingCore`、`OrderTracker` 所有权、`MakerPolicy` 策略逻辑、CAPITAL 禁用状态；不接第二家交易所；不做 TESTNET/LIVE 写。

---

## 决策统计

| 项 | 主题 | 建议 | 状态 |
|---|---|---|---|
| D1 | 预算口径 | Gross notional | 待确认 |
| D2 | 架构 | 方案 A（组合第二道闸） | 待确认 |
| D3 | 统计方式 | Gross 非 net | 待确认 |
| D4 | 组合限额字段 | 仅 `max_portfolio_gross_notional` | 待确认 |
| D5 | 拒绝 code | 新增 `PORTFOLIO_NOTIONAL_LIMIT` | 待确认 |
| D6 | 在途计入口径 | 持仓+ACTIVE/LOST 在途+本单，互斥不重计 | 待确认 |
| D7 | 在途限额作用域 | 正式 per-symbol + 修 engine 传参 | **待授权** |
| D8 | 并发 | 单决策线程顺序评估 | 待确认 |
| D9 | 限额是否分标的 | 全部 symbol 共用一组 | 待确认 |

> 你可以直接回复"D1–D9 按建议"一次性确认，或逐条改写。确认后我再产出 P0017.2 正式设计方案供批准，**在此之前不写实现代码、不改 currentProposal**。
