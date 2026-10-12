# P0017.4 Closure Audit（阶段收口审计）

**日期**：2026-10-12
**审计基线**：`70b4ff6`（实现 `2f52f0e` + 验收报告 `70b4ff6`）；detached clean checkout **2540 passed / 0 failed / 24 skipped**。
**性质**：只读（未改代码/状态；`currentProposal=P0017.4` 保持，待人类据本审计决定置 null）。
**输入**：原始设计 `proposals/P0017.4-deterministic-multi-symbol-portfolio-backtest.md`（**未跟踪**，人类建档）、验收报告 `artifacts/p0017/P0017_4_ACCEPTANCE_REPORT.md`、真实代码与测试复跑。
**分类**：A 正确性缺陷（收口前必修）；B 能力缺口（明确禁用边界+启用条件）；C 测试/证据增强（不影响正确性）。

---

## 1. 范围核对：对照原始 Batch/§7，有无缩小、降级或"做成别的东西"

逐条核对（不只引用验收报告，回到代码/测试）：

| 设计要求（§2.1/§6/§7） | 实现 | 判定 |
|---|---|---|
| §2.1.1 一个数据集含多个标的 | `_ordered_events` 接受多 symbol；`MultiBacktestConfig.symbols` | 覆盖（2 symbol 实测+测试） |
| §2.1.2 每标的独立市场/特征状态 | per-symbol `FeatureEngine` + per-symbol `SimulatedVenue`；`MultiSymbolSimulatedAdapter` 按 symbol 路由，`on_market_event` 对非本标的事件 raise（`venue.py:487`），不共享盘口 | 覆盖（结构隔离 + `test_two_symbols...`） |
| §2.1.3 共享组合风险预算 | 单 `RiskGate`/`OrderManager`/`AccountingCore` + P0017.2 portfolio gate（`portfolio_commitment_provider` 汇总跨标的持仓+在途） | 覆盖 |
| §2.1.4 订单/成交/持仓/费用组合一致 | 单 FillLedger/AccountingCore；summary 加 `per_symbol`；integrity `portfolio_gross_equals_sum_symbol_notionals` | 覆盖 |
| §2.1.5 同时间戳跨标的确定性规则 | `(exchange_ts, store ordinal, symbol)` 排序（`_ordered_events:78`）；单线程；实测 fixture 大量同 ts（BTC 块→ETH 块按 ordinal） | 覆盖，但见 C-2（预算竞争未专门断言） |
| §2.1.6 缺/无法估值非空仓位 fail-closed | 复用 P0017.2 L1；非 flat `notional is None`→unresolved→`PORTFOLIO_EXPOSURE_UNKNOWN` | 覆盖（引擎层测试 + commitment 逻辑） |
| §2.1.7 确定性/因果/风险/会计证明 | 双跑内容一致、追加未来事件前缀一致、守恒检查 | 覆盖 |
| §2.1.8 单标的语义/结果不非预期变化 | P0017.3 16 测试 + P0017.2/P0017.1 全绿；单标的仍走原 `run_backtest`（runner 仅在 `backtest.symbols` 存在时分流） | 覆盖 |
| §2.2 不做 bar/滑点/funding/taker/多策略/并行/实盘 | 均未实现，报告/产物显式 UNSUPPORTED；无 scope creep | 一致 |
| §4 event-store JSONL 输入 | 复用 `JsonlEventReader`，未强制 market-dataset | 一致 |
| §5 manifest/summary/report 多标的字段 | summary `symbols`+`per_symbol`；manifest config.canonical 含 symbols/per-symbol tick 表 | 覆盖 |

**结论：没有把多标的"做成别的东西"，也没有静默缩小 §2.1 的承诺。** 唯一与原始字面措辞的差异是 reduce-only 不变式（§2 详述），那是**澄清而非缩减**。

### 一处实现期发现并已修复的真实缺陷（记录，非遗留）
P0017.4 实施时发现 P0017.3 的 commitment provider 把**所有有 pending 的 symbol 误标 unresolved=True**（单标的组合限额启用时会误报 `PORTFOLIO_EXPOSURE_UNKNOWN`）。已在 `backtest/driver.py`（与 multi_driver 同步）修正为"仅真实 unresolved 订单 / 缺 mark 非 flat 持仓"。P0017.3 单标的测试此前因组合限额多为 None 未触发；修正后单标的 16 测试 + P0017.2 全绿，无回归。**这是本阶段应修尽修的 A 类问题，已闭环。**

---

## 2. reduce-only 不变式 vs 原始 C 类验收（你的第 2 点）

**原始 P0017.4 设计 §7-C 的字面表述**：
- "多标的累计暴露受组合限额约束"
- "待处理订单不能绕过预算"

**设计全文未出现 "reduce-only"**（grep 0 命中）。

**实现的语义**：reduce-only 挂单 `increment=0`，可越过 gross 上限；我把验收不变式表述为"accepted **非 reduce-only** 新增暴露 projected gross ≤ limit"。

**这是否偏离原始验收？结论：不偏离，但原始 C 表述需要澄清；实现正确。** 依据：
1. reduce-only increment=0 不是 P0017.4 新发明，而是 **P0017.2 已人类裁决并验收的语义**（`risk/portfolio_budget.py:11` 模块契约："候选单只在它增加风险时占用组合预算；真正的 reduce-only 不增加 projected gross"；纯函数 `_candidate_increment/projection` 对"方向相反且不超过净持仓"给 0，且对**超量 reduce-only/反手**保守全额计——`test_oversized_reduce_only_counts_full_notional`、`test_proven_reduce_only_adds_zero`）。
2. "待处理订单不能绕过预算"的安全含义是"不能通过下单让组合净风险超过预算"。reduce-only 在最坏情况下只降低某 symbol 的净敞口（且仅在 `|qty|≤|net_qty|` 时才 increment=0，超出部分按全额），故**不构成绕过**。
3. 实测（limit=500）复核：88 笔 reduce-only rests（SELL 50/BUY 38，方向与持仓相反），13 笔新增暴露 rests=156 ≤500；**期末净持仓 ≈0**（BTC ~0、ETH −0.0002）。若 reduce 真的能制造风险，期末净敞口不会归零。
4. P0017.4 没有重新定义这个语义（设计 §3.2 明确"不得重新定义 P0017.2 风险语义；如不足先报告差异"）。实现复用同一 `evaluate_portfolio_budget`，无第二条路径。

**需要澄清的是验收报告/设计的字面口径**，不是代码：C 类的"累计暴露"应明确为"**新增风险暴露（非 reduce-only 的 projected gross）**"，与 P0017.2 术语对齐；"所有在途名义之和 ≤ limit"不是也从未是 P0017.2 的不变式。验收报告已写明此澄清。**建议：在收口时把该术语澄清作为既定语义记录，不要求改代码。**

---

## 3. 状态一致性（你的第 3 点）

| 载体 | 现状 | 一致性 |
|---|---|---|
| 工作树 | 仅一个未跟踪文件（人类建档的 P0017.4 设计正文）；无未提交代码变更 | 干净（代码全部已提交） |
| HEAD / origin | `70b4ff6` = origin/master | 一致 |
| detached 复核 | 2540 passed（独立 worktree 重跑） | 一致 |
| `context/status.json` | `currentProposal=P0017.4` | 收口前正确（置 null 待确认） |
| `context/roadmap.md` | **无 P0017.4 行** | **收口时需补**（状态动作，非缺陷） |
| `context/current_state.md` | 无 P0017.4 收口段 | 收口时需补 |
| `proposals/P0017.4-*.md` | **未跟踪**（人类明确"由我编写"，与 P0017.3 归档前同样状态） | 待归档（人类此前对 P0017.3 的做法是收口后单独文档治理；建议沿用） |
| CLI 实跑 | 2 symbol×300 步 SUCCESS、1154 提交/697 成交、8 integrity 全 PASS | 与提交代码一致（用 `profiles/backtest-multi-symbol.json`，非测试专用旁路） |
| 是否"只在某一侧通过" | 无：driver/runner/report 均被集成测试与 CLI 真实调用；不存在只在 mock 通过的核心路径 | 一致 |

---

## 4. 测试与证据链（你的第 4 点）

- **2540 passed / 0 failed / 24 skipped**。24 skipped 全部是既有 opt-in 外部测试（`PROBEX_LIVE_*`/`JEV_LIVE_TEST`+key），与多标的回测无关。
- 新增 **7 个**多标的集成测试（`tests/integration/test_multi_symbol_backtest.py`），覆盖：
  1. 两标的进入同一回测且订单归属隔离；
  2. 组合限额对**新增暴露**生效（1023 次 `PORTFOLIO_NOTIONAL_LIMIT`，accepted 新暴露 ≤500）；
  3. 被拒单无成交/会计副作用（ledger==trace fills）；
  4. 数据含未声明 symbol → INVALID_INPUT；
  5. 声明了数据没有的 symbol → INTEGRITY_FAILURE；
  6. 双跑 (symbol,client_id,status,filled,avg_price) 序列 + equity + trace 全等；
  7. 追加 +2h 未来事件，订单内容/fills/equity 前缀全等。
- **回归**：P0017.3 单标的 16 测试、P0017.2 portfolio（`test_portfolio_budget`/`test_portfolio_gate_engine`/`test_exposure_by_symbol`）、P0017.1 全部绿。
- **CLI 2×300 的 8 项 integrity**：fills_match_ledger、equity_finite、pending_exposure_conservation、order_statuses_known、multi_pending_conservation、all_orders_belong_to_declared_symbols、every_declared_symbol_present_in_data、portfolio_gross_equals_sum_symbol_notionals——全部 PASS，足以支撑组合/标的勾稽。

**结论：证据足以支撑 closure 的核心主张**（隔离、组合预算不被多标的绕过、会计勾稽、可复现/因果、失败分类、单标的无回归）。

---

## 5. 风险与"通过但脆弱/依赖澄清/未覆盖"项（你的第 5 点，不淡化）

- **C-1 reduce-only 术语澄清（收口动作，非代码）**：见 §2。需在 roadmap/current_state/验收口径把"累计暴露"明确为"新增风险暴露"，避免未来读者用"所有在途名义之和"误判。**不澄清会造成文档-代码语义表面冲突。**
- **C-2 同时间戳预算竞争无专门断言**：设计 §3.3/§98 明确"同 ts 顺序会影响谁先占剩余预算，属策略实验语义"。实现用 store ordinal 确定性 tiebreak，且双跑内容一致**间接**锁定了结果可复现；但没有一条测试专门构造"同 ts、预算只够一个标的"并断言**哪个标的胜出**。确定性已证，**业务期望的胜出者**未被钉死。建议补一条小测试（成本低），不补也不影响正确性（规则已显式且可复现）。
- **C-3 仅测 2 个标的**：设计要求"至少两个"（§6 Batch1 line 189），2 个满足强制标准；3+ 标的未测。fan-out 是 dict 泛化实现，风险低但无直接证据。
- **C-4 per-symbol 仅 tick/step 可分**：其他 maker/risk 参数所有标的共用（与 P0017.2 D9 一致，验收报告已声明）。若未来要 per-symbol 全套策略参数，需扩展——当前是 B 类已知边界，不是缺陷。
- **B-1 真实多标的行情 E2E**：仍受 Binance 451 约束（P0017.1 外部 B 类）；离线确定性已证，真实数据未证。
- **B-2 Testnet/LIVE 多标的组合回测/写路径**：结构性不可达（AST/import 隔离），不适用也未授权。
- **观察（非本阶段引入）**：`_predict` 改为每次新建事件循环（为修多标的交错 stale）。单线程顺序使用，无并发问题；高标的数×长数据会有轻微循环创建开销，属性能项，不影响结果正确性。

**无 A 类（正确性）缺陷。** 实现期发现的 unresolved 误判已修复并回归。

---

## 6. 收口判定


**结论：建议收口（可在完成两个零风险收口动作后置 `currentProposal=null`）。**

理由：
1. §2.1/§6/§7 的承诺能力全部闭环，无静默缩减、无"做成别的东西"；2540 全绿（工作树+detached）。
2. reduce-only 不是实现偏离，而是 P0017.2 已裁决语义在多标的下的正确复用；需要的是**术语澄清**（C-1，文档动作），不是改代码。
3. 组合预算跨标的不被绕过、会计勾稽、状态隔离、确定性/因果、失败分类、单标的无回归均有真实引擎级/CLI 证据。
4. 剩余项均为 C（同 ts 竞争专门断言、3+ 标的）或 B（真实行情、Testnet/LIVE），有明确边界，不会被误认为已验证。

**建议收口时一并（不改交易代码）**：
- roadmap 增 P0017.4 行、current_state 增收口段、`currentProposal→null`；
- 记录 reduce-only 术语澄清（C-1）；
- 把 C-2（同 ts 预算竞争胜出断言）、C-3（3+ 标的）登记为后续 C 类测试增强（可在收口后小批补，不阻塞）；
- P0017.4 设计正文的归档按 P0017.3 既有做法，由人类决定后单独文档治理，不在本次混入。


---

## 7. 收口执行记录（2026-10-12，人类批准路径）

**第一阶段 C-2 已完成并通过**（未改生产/风险/排序代码）：
- 新增 `test_same_timestamp_budget_contention_winner_is_deterministic`：同时间戳 BTC/ETH 各提交 400 新增暴露、组合限额 500，
  store ordinal 较小的 **BTC 确定性胜出**（唯一 OPEN 400），ETH 被 `PORTFOLIO_NOTIONAL_LIMIT` 拒，
  0 成交、equity 不变（拒绝无副作用），双跑订单与拒绝轨迹完全一致。
- 全量 **2541 passed / 0 failed / 24 skipped**。

**第二阶段已执行**：currentProposal→null；roadmap/current_state/handoff 更新；C-1 术语澄清入档；
C-3（3+ 标的专项）登记为独立 C 类增强；真实多标的行情 E2E（451）、Testnet/LIVE 写路径保持 B 类未验证。

**最终结论：P0017.4 正式收口（离线多标的确定性组合回测）。** 无 A 类正确性缺陷；reduce-only 语义为 P0017.2 既有裁决；
未运行的真实/实盘场景保持 NOT RUN，不计 PASS。第三阶段文档归档另行处理（设计正文本次仍未跟踪）。
