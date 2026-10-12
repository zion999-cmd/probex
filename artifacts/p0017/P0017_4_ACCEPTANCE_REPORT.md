# P0017.4 Acceptance Report — Deterministic Multi-Symbol Portfolio Backtest

**Date**：2026-10-12
**Implementation commit**：`2f52f0e`（pushed）
**Verification**：`python3 -m unittest discover -s tests -t .` → **2540 passed / 0 failed / 24 skipped**；detached clean checkout at `2f52f0e` 复核一致。
**Scope**：offline deterministic only；无网络/凭据/交易所写。

Legend：**[T]** automated ｜ **[S]** static ｜ **[X]** external/not run。

---

## 1. 交付物

| Deliverable | Location |
|---|---|
| 多标的模拟执行适配器 | `backtest/multi_adapter.py::MultiSymbolSimulatedAdapter`（fan-out/in per-symbol SimulatedVenue） |
| 多标的配置/driver | `backtest/multi_driver.py::MultiBacktestConfig / run_multi_backtest` |
| 单标的 driver 修正 | `backtest/driver.py`（commitment unresolved 误判修正；每预测独立事件循环） |
| runner 自动路由 | `backtest/runner.py`（`backtest.symbols` 存在即多标的，per-symbol MakerPolicy） |
| 多标的报告字段 | `backtest/report.py`（`symbols`、`per_symbol` 明细） |
| profile | `profiles/backtest-multi-symbol.json` |
| 测试 | `tests/integration/test_multi_symbol_backtest.py`（7） |

复用未复制：FeatureEngine、SimulatedVenue、ExecutionEngine/OrderManager/OrderTracker、AccountingCore/FillLedger、RiskGate、P0017.2 `risk/portfolio_budget`、MakerPolicy、ReplaySource、P0017.3 identity/manifest/report。

---

## 2. 强制验收（§7 A–F）

### A. 输入与事件契约
| 标准 | 结果 | 证据 |
|---|---|---|
| 多标的输入正确解析 | **[T]** | `test_two_symbols_run_in_one_backtest_with_isolation`（两 symbol 订单/成交各自归属） |
| 非法/不完整输入被拒 | **[T]** | `test_unknown_symbol_in_data_is_rejecteded`（数据含未声明 SOL→INVALID_INPUT）；缺文件/乱序沿用 P0017.3 |
| 同时间戳排序确定 | **[T]** | `(exchange_ts, store ordinal, symbol)` 排序（`_ordered_events`），单线程；无 hash/线程依赖 |
| 标的状态隔离 | **[T]** | 每 symbol 独立 FeatureEngine + SimulatedVenue；`test_two_symbols...` 订单全部只属于声明标的 |

### B. 因果与确定性
| 标准 | 结果 | 证据 |
|---|---|---|
| 无 look-ahead | **[T]** | SimulatedVenue entry_ts no-future；每标的独立状态，未来事件只路由给对应 venue |
| 同输入双跑核心一致 | **[T]** | `test_reproducible_two_runs`（订单 (symbol,id,status,filled,avg) 序列、equity、trace 全等） |
| 追加未来事件不改前缀 | **[T]** | `test_future_event_preserves_prefix`（订单内容前缀、fills、equity 全等；未来事件只增加其后订单） |
| 输入顺序/规范化一致 | **[T]** | 时间线严格单调校验（逆序→INVALID_INPUT），ordinal tiebreak |

### C. 组合风险（核心）
| 标准 | 结果 | 证据 |
|---|---|---|
| 多标的累计暴露受组合限额约束 | **[T]** | `test_portfolio_budget_blocks_cross_symbol_accumulation`（limit=500 时 1023 次 `PORTFOLIO_NOTIONAL_LIMIT`；**新增暴露** accepted 名义 ≤500） |
| 待处理订单不能绕过预算 | **[T]** | commitment provider 汇总 accounting 持仓 + tracker `exposure_by_symbol`（P0017.2 D7）；多标的通过同一引擎提交 |
| 风险拒绝无副作用 | **[T]** | `test_rejected_orders_have_no_accounting_side_effect`（ledger==trace fills；被拒单不在 tracker） |
| 未知估值不低估风险 | **[T]** | 非 flat 缺 mark→`PORTFOLIO_EXPOSURE_UNKNOWN`（复用 P0017.2 L1）；普通 pending 不再被误标 unresolved（本次修正，单标的回归全绿） |

> **重要语义澄清（设计 §3.2 与 P0017.2 一致）**：reduce-only 挂单按既定 P0017.2 语义**可越过 gross 上限**（它们最坏只降低净敞口，increment=0）。因此可验证的不变式是"**accepted 的非 reduce-only 新增暴露 projected gross ≤ limit**"，不是"所有在途名义之和 ≤ limit"。实测 limit=500：新增暴露 OPEN=156（≤500），reduce-only rests 虽达 1058 但期末净持仓 ≈0。这是设计行为，不是绕过。

### D. 会计与执行
| 标准 | 结果 | 证据 |
|---|---|---|
| 订单/成交/持仓/费用一致 | **[T]** | `fills_match_ledger`、`portfolio_gross_equals_sum_symbol_notionals`、`multi_pending_conservation` 全部 PASS |
| 组合/标的汇总可核对 | **[T]** | summary `per_symbol`（orders/fills/filled_quantity/end_position）；integrity 校验组合 gross=Σ symbol 名义 |
| 终态语义正确 | **[T]** | 末尾统一 fan-in poll；不虚构收盘价/强平；OPEN 挂单为合法被估值终态 |
| 失败不被报告为成功 | **[T]** | 任一 integrity 失败→INTEGRITY_FAILURE；缺 symbol（声明 SOL 但无数据）测试覆盖 |

### E. 运行隔离与可复现
| 标准 | 结果 | 证据 |
|---|---|---|
| 离线无需凭据/网络 | **[T]** | 全部输入本地 JSONL；无 connectors/网络 import |
| Testnet/LIVE 写路径结构不可达 | **[T]** | P0017.3 AST 守卫覆盖 `backtest/`（multi_adapter/multi_driver 同样只依赖本地模拟组件）；不 import runtime.testnet/assembly/connectors |
| 身份可追溯 | **[T]** | run identity 含 symbols/per-symbol tick/step 的 canonical config（改标的集合→身份变） |

### F. 兼容与收口
| 标准 | 结果 | 证据 |
|---|---|---|
| 单标的无意外回归 | **[T]** | P0017.3 全部 16 测试 + P0017.2/P0017.1 测试全绿（2540 全量） |
| 能力声明与实际一致 | **[T]** | 见 §4；不把多标的能力外推到实盘 |
| B/C 类有记录 | **[T]** | §4 |
| Closure Audit | 待做 | 本报告为输入，按 §26.1 收口审计 |

---

## 3. 实跑证据

- CLI：`probex backtest --dataset <2 symbol 300-step> --config profiles/backtest-multi-symbol.json --out ...`
  - status SUCCESS；submits 1154；fills 697（BTC 353 / ETH 344）；
  - 8 项 integrity 全 PASS（含多标的守恒、归属、组合 gross=Σ 名义）；
  - summary 含 `symbols=['BTCUSDT','ETHUSDT']` 与 `per_symbol` 明细。
- 限额压测（500）：新增暴露受限于 ≤500，1023 次组合拒绝，期末净持仓≈0。
- detached clean checkout：2540 passed。

---

## 4. 能力边界（不扩大结论）

1. v1 多标的**共享同一 maker 参数集**（per-symbol tick/step 可通过 `backtest.tick_size_by_symbol`/`quantity_step_by_symbol` 覆盖；其他 maker/risk 参数共用，与 P0017.2 D9 一致）。
2. 仍是 **event-driven**（非 BAR/OHLCV）；slippage/funding/taker/market-impact 仍 UNSUPPORTED。
3. 回测离线、结构性不可达 Testnet/LIVE；多标的**真实行情** E2E 仍受 Binance 451 约束（P0017.1 B 类）。
4. 不做资本在多策略间分配、不做参数优化/并行回测（设计 §2.2）。
5. reduce-only rests 不计入 gross 上限是 P0017.2 既定语义（见 C 节澄清），不是缺陷。

## 5. 残留
- 无 A 类正确性缺陷。
- 后续（独立提案）：per-symbol 全套 maker 参数、bar 粒度、真实成本模型、多标的真实行情 E2E、run registry 归档。
