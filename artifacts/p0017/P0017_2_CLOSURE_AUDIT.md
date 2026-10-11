# P0017.2 Closure Audit（阶段收口审计）

**日期**：2026-10-11
**基线**：commit `14b69fc`（含 L1 修正）；detached clean checkout **2517 passed / 0 failed / 24 skipped**
**性质**：范围受限的最终收口审计（人类 2026-10-11 要求）；本文件不改代码、不改 `currentProposal`（保持 `P0017.2`，待人类据本审计决定置 null）。

**遗留分类法（本项目强制规则）**
- **A 正确性缺陷**：可能导致错误决策/记账/风险约束失效/错误结果 → 收口前必须修复。
- **B 能力缺口**：未覆盖某路径，但系统明确限制该路径且不会被误认为已支持 → 可延后，但必须记录禁用边界与启用条件。
- **C 优化项**：不影响正确性/风险/可信度/接口契约 → 进优化计划。
- 规则：不得把 A 重命名为 C；未验证能力不得当作已具备。

---

## 1. 承诺闭环（本阶段必须实现的能力是否完成？有无未通过 SC？）

| 承诺（设计 §3.1 / D1–D9） | 状态 | 证据 |
|---|---|---|
| 跨标的共享 gross notional 限额 | 完成 | `risk/portfolio_budget.py`；`test_two_symbols_each_ok_but_portfolio_over_limit_rejected`（6000+6000>10000） |
| 保留现有 RiskGate 单标的职责 | 完成 | gate 未改判定数学；symbol mismatch 守卫保留（全量绿） |
| 持仓 + 在途订单统一纳入预算，互斥不重复 | 完成 | `remaining=qty−filled`；持仓/在途分桶；`test_pending_orders_count...`、引擎 `..._pending_order_consumes_portfolio_budget_before_fill` |
| 单标的在途限额作用域明确为 per-symbol（D7） | 完成 | `OrderTracker.exposure_by_symbol`、`OrderManager.*_for_symbol`、engine snapshot 改 per-symbol；守恒测试 |
| 新限额默认关闭、配置兼容（D4） | 完成 | `max_portfolio_gross_notional=None`；`test_disabled_when_limit_none` + 2517 全量零回归 |
| 单决策线程顺序评估（D8） | PAPER 完成 | PAPER 唯一新增暴露入口为 `RuntimeDecisionLoop`（单线程同步 submit）；Testnet 是独立路径（见 §3） |
| 缺 mark / 不确定状态 fail-closed（L1 / §12） | 完成（已修正） | 见 §2 |

**结论：P0017.2 承诺能力全部闭环；无未通过的离线验收标准。**
唯一带 [X] 的是设计本身就划在范围外的真实/外部路径（§3、§4），不是本阶段承诺。

## 2. 正确性闭环（是否还有影响决策/风险/会计/可信度的已知缺陷？）

- **L1（A 类，已修复）**：缺 mark 非 flat 持仓曾被静默跳过 → 跨标的低估组合 gross。修复：provider 标记该 symbol 不可量化，组合闸 `PORTFOLIO_EXPOSURE_UNKNOWN`。4 项跨标的/恢复/flat 测试 [T]。复核 `runtime/assembly.py:1586-1605` 与 `risk/portfolio_budget.py`，确认无第二条"跳过 None 名义"的路径：
  - 组合纯函数层：unresolved 由调用方提供（诚实）；
  - assembly 层：非 flat + `notional is None` → unresolved（已覆盖）。
- 成交/会计一致性：`on_events` 同一临界区先 tracker 后 accounting（`execution/engine.py:211-213`）[S]；重复/迟到成交由既有双键去重保证，本次未改该路径，相关既有测试全绿 [T]。
- 候选单只计一次：组合闸在 `manager.submit` 之前判定，拒绝不登记；通过才登记为 ACTIVE [T]（引擎测试断言 `SOLUSDT not in tracker.orders`、在途成交前占预算）。

**结论：无遗留 A 类正确性缺陷。**

## 3. 能力边界（哪些路径支持/不支持？会不会被误认为已支持？）

| 路径 | 是否有组合预算保护 | 系统是否会误表现为已支持 |
|---|---|---|
| PAPER（单决策线程，`runtime/assembly.py`） | **是**（配置限额时）；未配置则该闸关闭但语义与变更前一致 | 不会：限额 None 是显式"未启用"，trace/capabilities 不宣称 |
| REPLAY | 不产生新风险（只观察/重放历史），不需要组合闸 | 语义清楚 |
| **Testnet（`runtime/testnet.py:795`）** | **否**：自建 ExecutionEngine，未传 `portfolio_commitment_provider`（grep 计数 0） | **风险点**：即使配置了组合限额，Testnet 引擎也不会执行组合闸。单标的 RiskGate 仍生效。已在验收报告与本审计明确记录；产品面无任何"Testnet 组合已保护"的声明 |
| **LIVE 主网写** | 否（本就无授权、无路径） | 不会：写路径受 readiness authority 门控 |
| 真实多标的行情驱动 | 组合闸离线确定性已验证；真实行情 E2E 受 Binance 451 阻塞 | 不会：P0017.1 已标"真实 E2E NOT RUN"，UI 失败显示真实错误 |

**Testnet 边界的处置结论（B 类）**：明确禁用——组合预算在 Testnet/LIVE **未接线、未验证**；启用条件 = 独立提案 + 写授权 + 多标的真实持仓/挂单 commitment provider + 专项验收。不得由 PAPER 结果外推。

## 4. 遗留项处置（逐项定性 + 归属）

| 项 | 分类 | 为何可延后 / 如何处理 | 归属 |
|---|---|---|---|
| **L2：MakerPolicy sizing 不感知组合限额** | **C 优化项（已重判，附证据）** | 见下方专门论证；不影响正确性/风险边界 | 后续优化（不阻塞 P0017.3） |
| Testnet/LIVE 未接组合预算 | **B 能力缺口** | 路径独立、默认无写授权、明确未接线；启用需独立提案+授权 | 未来"真实多标的"提案 |
| 真实多标的行情 E2E（451） | **B（外部验证缺口）** | 离线确定性已证；真实证据待非受限出口；NOT RUN 已记录，禁止用离线替代 | P0017.1 收尾（出口具备时） |
| reduce-only 与其他在途单共存无专门断言 | C（测试覆盖） | 语义由 gross 全计入覆盖；可补一条专门测试增强信心 | 可选优化 |
| 24 skipped（live/external opt-in） | 非缺陷 | 全部需真实网络/凭据，与组合路径无关 | 外部条件 |

### L2 为什么是 C 而不是 B/A（重点重判，回应人类关切）

事实链（全部代码核实）：
1. 策略 sizing 经 `strategy/maker/sizing.py::_resolve_budget_factor` 消费 `remaining_risk_budget`；该值由 `runtime/assembly.py::_risk_budget` → `risk/budget.py::remaining_exposure_budget(snapshot, limits)` 计算。
2. `grep portfolio risk/budget.py` = **0**：sizing 预算**不含** `max_portfolio_gross_notional`，因此策略不知道组合剩余额度。
3. **但越界订单不可能成交**：`ExecutionEngine.submit` 的组合第二道闸在登记前强制执行（A 类安全已由 §1/§2 保证）。策略最坏行为是：继续按单标的额度报价 → 这些单在 submit 被 `PORTFOLIO_NOTIONAL_LIMIT` 拒绝 → 产生 reject（计入 decision log/trace，UI 可见），**不产生订单、不占预算、不改变持仓/会计/风险结果**。

判定：
- 不是 **A**：无错误决策被执行、无错误记账、风险约束不失效（硬闸兜底）。
- 不是 **B**：系统没有"限制某路径却看似支持"——它会**显式拒绝**并给原因，行为正确，只是不优雅。
- 是 **C**：影响的是报价效率/策略信号质量（生成注定被拒的意图）与 reject 噪声，不影响正确性、风险边界、结果可信度或对外接口契约。

启用/处置条件：当多标的 PAPER 实际运行且 reject 噪声影响观测/策略表现时，让 `remaining_exposure_budget` 增加一个组合候选约束（取 min），属策略 sizing 调整，须单独改动并补测试（届时需复核它与单标的候选的取 min 语义）。**P0017.3 回测复用的是 RiskGate + 组合硬闸语义（已验证），不依赖 L2**，因此 L2 不阻塞回测。

## 5. 状态一致性（代码 / 测试 / 报告 / roadmap / currentProposal）

| 载体 | 内容 | 一致？ |
|---|---|---|
| 代码 | 组合闸 + per-symbol 在途 + L1 fail-closed | 是 |
| 测试 | 2517 passed（工作树 + detached）；新增组合相关 22 项 | 是 |
| 报告 | 验收报告（§0.3 L1 已更新为"已修复"）+ 本 Closure Audit | 是 |
| `proposals/P0017.2-*.md` | 状态=已批准并实施+L1 修正，等待收口 | 是 |
| `context/roadmap.md` | P0017.2=组合 gross 预算已实施 | 是（措辞需在正式收口时改为"已完成（离线）；Testnet 未验证"） |
| `context/status.json` | `currentProposal=P0017.2` | **保持，待人类确认后置 null** |
| `artifacts/p0017/P0017_2_DECISIONS.md` | 决策依据 | 是 |
| P0017.3 文档 | 未跟踪，未提交/删除/改名（遵人类指示） | 是 |

---

## 收口建议

五问全部可正面回答：
1. 承诺能力闭环、无未通过 SC；
2. 无遗留正确性缺陷（L1 已修）；
3. Testnet/LIVE 组合预算明确未接线、不会被误认为已支持；
4. 遗留项已逐项分类（L2=C 有证据；Testnet=B；真实 E2E=B 外部），均有处置/启用条件；
5. 状态一致，仅 `currentProposal` 与 roadmap 一处"待收口"措辞需在确认时更新。

**建议：人类确认后正式收口 P0017.2**——`currentProposal→null`、roadmap/current_state 标注"离线验收完成；Testnet/LIVE 组合预算与真实多标的行情 E2E 未验证（B 类，外部/授权前置）"，然后推进 P0017.3（其复用已验证的 RiskGate + 组合硬闸，不依赖 L2）。
