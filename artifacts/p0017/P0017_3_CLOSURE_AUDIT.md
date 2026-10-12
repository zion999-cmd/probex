# P0017.3 Closure Audit（阶段收口审计）

**日期**：2026-10-12
**审计基线**：`8d2c47a`（实现 `03d9081` + 验收报告 `8d2c47a`）；detached clean checkout **2533 passed / 0 failed / 24 skipped**。
**性质**：只读（未改代码/状态；`currentProposal=P0017.3` 保持，待人类据本审计决定置 null）。
**输入**：最终设计 `proposals/P0017.3-offline-backtest-driver-evaluation-report.md`（未跟踪）、验收报告 `artifacts/p0017/P0017_3_ACCEPTANCE_REPORT.md`、真实代码与测试复跑。

**分类**：A=正确性缺陷（收口前必修）；B=能力缺口（明确禁用边界+启用条件）；C=优化/证据增强（不影响正确性）。

---

## 1. 承诺闭环：35 项 SC 逐项复核（不照抄验收报告，回到代码与测试）

### 1.1 必须报告的两处证据缺口（非功能缺陷）

- **C03「组合 gross 不可被多标的累计绕过」在回测内未被直接测试。**
  - 代码事实：driver 在输入层拒绝多 symbol（`backtest/driver.py:140` `event.symbol != symbol → INVALID_INPUT`，错误信息明确 "single-symbol event driver"）；内部 commitment provider 却按 `accounting.positions` 跨 symbol 聚合（`:183`），引擎组合闸本身可跨标的。
  - 结论：v1 **结构性不可能发生**"多标的累计绕过"——输入只允许一个 symbol，跨标的累计场景无法进入回测。跨标的组合硬闸的**正确性由 P0017.2 测试保证**（`test_two_symbols_each_ok_but_portfolio_over_limit_rejected`、`test_..._pending_order_consumes_portfolio_budget_before_fill`）。
  - 判定：**B 类能力边界（多标的回测未实现），不是 A**：绕过路径在 v1 不可达。但验收报告把 C03 标为 [T] 略强，准确表述应是"**通过输入单标的约束使该场景不可达 + 复用的组合闸经 P0017.2 验证**"。需在报告中改正措辞。
- **未来事件追加测试（B03）断言偏弱**：现有 `test_future_event_does_not_change_prior_decisions` 只断言 `decisions >= ` 计数不等式与 rejects_log 前缀，**未断言订单内容/成交/equity 的前缀一致**。
  - 我独立复跑验证（同一 run_id）：追加 +2h 事件后，前 1576 单的 `(status, filled_qty, avg_price)` 内容前缀**完全相等**、equity 相等（999996.399…）、fills 相等；ext 多出 2 单 OPEN（由未来事件合法产生）。**因果不变性实际成立**，只是测试断言没有把它锁死。
  - 判定：**C 类（测试证据应增强）**，不是正确性缺陷。建议把断言升级为"同 run_id 下订单内容/成交/equity 前缀相等"（建议本收口前补上，成本低、直接锁住 §5.4）。

### 1.2 逐项（简表，证据均已复跑或读码确认）

- **A01–A06**：A01/A02/A04/A05/A06 [T]；**A03**：见 §1.3——bar 在**事件 schema 层就无法构造**（`EventType.BAR` 无 payload 映射，`event_type_for(dict)` 抛 `InvalidEventError`，实测），故 bar-only 输入不可能进入合法 event-store；driver 定义了 `UNSUPPORTED_CAPABILITY` 但**没有任何路径 raise 它**（`grep` 确认）。判定：能力上 bar 被 schema 挡死（等价于不支持），但**命名状态不可达**——**C 类**：要么补一个针对非法/不支持载荷返回 UNSUPPORTED_CAPABILITY 的分支与测试，要么在报告中把 A03 的满足依据明确写为"输入契约层排除"。建议后者（最小、诚实）。
- **B01/B02/B04/B05/B06/B07**：[T]（单线程同步、exchange_ts 单调校验、双跑 trace/orders/equity 全等、venue 平局 `(entry_ts,client_order_id)`、尾部 poll、integrity 失败→INTEGRITY_FAILURE/非成功退出）。
- **C01/C02**：[T]（engine.submit 必经 RiskGate；commitment provider 注入；正常 fixture 产出真实 risk rejects）。
- **C03**：见 1.1（B + 复用 P0017.2 证据；报告措辞需修正）。
- **C04**：[T]（commitment provider 对非 flat 缺 mark 置 unresolved → 组合闸 fail-closed；L1 逻辑与 P0017.2 同一实现，`test_missing_mark...` 在引擎层）。
- **C05/C06**：[T] `test_risk_rejected_orders_never_enter_tracker_or_accounting`（被拒单 tracker 状态集只含已接受状态；fills 仅来自接受单）。
- **C07/C08**：[T] AST 扫描 backtest/ 无 `runtime.testnet`/`runtime.assembly`/connectors 写模块；运行时 import 复核仅领域层 + stdlib + `runtime.maker_config`（纯配置构造器，非写路径）。
- **D01/D03/D05/D06**：[T]（integrity `fills_match_ledger`、maker fee 单次、状态枚举校验、缺 mark 不填零）。
- **D02/D04**：[T，复用既有契约]（AccountingCore/OrderManager 未复制；守恒与部分成交由 P0017.2/既有 accounting 测试覆盖，回测走同一 submit/poll/record_fill 路径）。
- **D07**：[T 机制]（任一 integrity check false → status 置 INTEGRITY_FAILURE；当前 fixture 全 PASS）。
- **D08**：见 §1.3——`INCOMPLETE` 定义但无产生路径；driver 同步消费全部事件 + 末尾 poll，**不存在"未处理事件"**；fixture 末尾有 OPEN 挂单属正常（maker 挂单被估值，不构成未结算）。判定：**C**——应在设计/报告中显式声明"v1 同步全量处理使 INCOMPLETE 不可达；末尾挂单是合法终态"，避免该状态看似悬空。
- **E01–E09**：[T]（manifest/run_id/双跑 result_hash/输入变化改 id/summary↔report 同 run_id/非成功退出 30/UNSUPPORTED 成本声明/模型边界免责声明）。result_hash 覆盖范围已核：`canonical_json(summary)`，含 trace（decisions_log/rejects_log/cancels/fills/submits/events）、portfolio 终态、orders_by_status、integrity、capability 声明；**不含** wall-clock/临时路径（这些不进 summary，符合 §9.3）。**注意**：result_hash 不含**完整订单/成交明细列表**（只含聚合），等价性靠 trace+equity+orders 内容双跑断言（B04 测试有），建议知悉。
- **F01–F07**：[T]（CLI 退出码 0/2/30、独立 out 目录、核心逻辑在 driver 可独立测、detached 全绿、无 skip 换通过）。

### 1.3 未使用状态/未支持能力是否会被误认为已支持

- `UNSUPPORTED_CAPABILITY`（A03）、`INCOMPLETE`（D08）两个枚举值在 v1 **无产生路径**。它们不会让用户**误以为能力存在**（bar 输入在 schema 层即被拒；同步回测不会半截结束），但属于"定义了未接线"的死枚举。收口建议：在设计/报告显式记录其 v1 语义（schema 排除 / 同步不可达），不假装可从 API 观察到。**不阻塞正确性。**

## 2. 正确性闭环（决策/风险/会计/结束）

- 事件时间与可见性：策略 state 来自"截至当前事件"的 FeatureEngine；mark 单独进 accounting、不进 order book；venue 只收非 mark 事件，成交由**后续**事件驱动（`entry_ts` no-future）。结构上无 look-ahead，并经独立追加事件复跑验证前缀一致（§1.1 B03）。
- 订单生命周期：完全复用 tracker/manager/SimulatedVenue，未新建状态机；被拒单不登记（C05）。
- 风险副作用：risk reject 只记 trace，不产生订单/成交/费用。
- 成交↔会计：走既有 `engine.poll → manager → FillLedger → AccountingCore`；integrity 校验 ledger==trace fills。
- 结束：同步全量 + 尾部 poll；不虚构收盘价/不强平（§5.6）。
- **结论：无 A 类正确性缺陷。** 两个改进项（B03 断言增强、A03/D08 语义落文档）均为 C。

## 3. 确定性（§审计点 3）

- result_hash 字段覆盖见 1.2 E05；双跑 trace/equity/订单内容全等（B04 [T] + 我独立复跑）。
- 预测用 LocalTrialProvider（无随机性）+ FixedClock；driver 无 `time.sleep`/Thread/wall-clock（读码确认）。
- 真实 git commit 进入 manifest 但**不**进入 result_hash/run_id 的语义核对：`run_identity` 实际**包含** `code_version`（`backtest/identity.py`），故跨提交 run_id 会变；而 `result_hash` 只哈希 summary（不含 git commit），跨提交同结果 result_hash 稳定。二者分工合理，**报告中"git 不进 result_hash"表述正确，但 run_id 随代码版本变是预期**（§9.2 要求）。
- 改进：B03 未来事件测试应升级为内容前缀断言（C）。

## 4. 安全边界（§审计点 4）

- AST 测试覆盖 `backtest/**.py` 的 import（结构性，非"没配 key"经验判断，满足 C08）。
- 运行时复核：`runner.py` import `runtime.maker_config`（配置→策略工厂，不启动服务/不触网）；`prediction.py` import LocalTrialProvider（本地确定性）。**无** connectors、testnet/assembly、http client、凭据读取。
- CLI `backtest --help` 无 key/secret/live/testnet 参数（F 测试断言）。
- **结论：Testnet/LIVE 写路径在构造层不可达，隔离充分。**

## 5. 能力边界（§审计点 5，重点：单标的是否缩小承诺）

- **设计原文未声明单标的或多标的**（全文仅在输入校验提到 "symbol"）。但 C03 写了"多标的订单累计不可绕过组合预算"。
- 实现交付为**单标的 event-driven 回测**，并在输入层显式拒绝多 symbol（错误信息清楚）。
- 这构成一次**范围明确化（scope clarification），但严格说是对未写明范围的收窄**：设计没有授权"只做单标的"，不过：
  1. 它不造成任何**错误结果或风险绕过**（多标的输入被拒，不会静默按单标的处理）；
  2. 复用的组合闸已在 P0017.2 独立验证；
  3. 多标的回测是清晰、可增量加入的 B 类能力（driver 需 per-symbol FeatureEngine/SimulatedVenue/决策上下文）。
- 处置建议（需人类确认，不自行扩大）：在 P0017.3 收口记录中**明确把"多标的离线回测"登记为 B 类已知能力边界及启用条件**，并把 C03 的验收措辞改为"v1 输入单标的使累计绕过不可达；跨标的组合预算复用 P0017.2 已验证硬闸"。这样不把"未实现"说成"已验证"，也不把它误判为缺陷。
- 其他边界均显式声明：event-driven only、bar=schema 层不支持、slippage/funding/taker/market-impact=UNSUPPORTED、队列近似未校准、回测≠实盘收益（manifest/summary/report 三处一致）。

## 6. 状态一致性（§审计点 6）

| 载体 | 现状 | 一致？ |
|---|---|---|
| 代码/测试 | 2533 passed（detached 复核） | 是 |
| 验收报告 | A03/C03/D08 有措辞偏强（见 §1、§5） | **需小修措辞**（C，不改代码） |
| `context/status.json` | currentProposal=P0017.3（实施中） | 收口时处理 |
| `context/roadmap.md` | P0017.3 仍写"未批准（回测与评估报告）" | **过期**：已批准并实施；收口时更新 |
| `context/current_state.md` | 无 P0017.3 收口段（grep 命中为 0 实质内容） | 收口时补 |
| 设计文档 | 未跟踪、自标"DESIGN REVISION 待批准"，且自称 target `artifacts/.../P0017_3_DESIGN.md`（不存在） | 路径/状态待按人类既定布局归档（见下） |
| 两个未跟踪 md | 设计正文 + 差距审计，人类指示**暂不提交/改名/删除** | 遵守 |

文档归档（人类已定布局，收口时执行，不现在做）：正式设计与授权 → `proposals/`；差距审计/验收报告/Closure Audit → `artifacts/p0017/`。建议把未跟踪设计正文纳入版本控制（重命名为规范名或保留现名均可，待人类定），并修正其 target 路径与状态行。

---

## 7. 收口判定与建议

**五问结论**：
1. 承诺闭环：除 §1 三处 C 类证据/措辞项外，35 项离线 SC 满足；**无未通过的强制标准**。
2. 正确性闭环：**无 A 类缺陷**。
3. 能力边界：单标的为**需明示登记的 B 类范围收窄**（不静默、不错误）；bar/slippage 等均显式 UNSUPPORTED，不会被误认为已验证。
4. 遗留处置：建议（a）把 B03 未来事件测试升级为内容前缀断言；（b）报告 A03/D08/C03 改为准确措辞；（c）roadmap/current_state/设计文档状态在收口时统一。
5. 状态一致性：roadmap 过期、设计文档未跟踪——收口动作已明确。

**建议路径（二选一，人类决定）**：
- **路径 1（推荐）**：先做 §7.4 的 (a)+(b) 两个低成本 C 类加固（不改变任何行为，只加断言/改文档措辞），再正式收口 P0017.3（currentProposal→null、roadmap/current_state 更新、文档归档）。
- **路径 2**：接受当前证据强度（因果性已独立复跑成立、缺口仅为测试断言/措辞），直接收口，把 (a)(b) 记入后续 C 类清单。

**不建议**在未确认单标的边界措辞前直接宣称"P0017.3 全部 SC [T] PASS"——C03 的多标的语义应按 §5 诚实表述。
