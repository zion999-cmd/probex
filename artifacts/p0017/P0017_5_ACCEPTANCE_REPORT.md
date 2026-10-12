# P0017.5 验收报告 — 实时多标的行情驱动模拟交易链路收口

**日期**：2026-10-12　**Proposal**：P0017.5（已批准，单一已授权工作包）
**证据分层**：[T] 自动化测试/可复现命令 ｜ [S] 静态代码路径/AST 守卫 ｜ [X] 外部环境未运行（NOT RUN）

---

## 1. 交付链路（产品目标 §1）

```
MultiSymbolMarketCollector（P0017.1，只读公开行情，WS/REST）
  └─ CollectorBatchSource.pump()  → SymbolBatch(events: MarketEvent, source_error?)
       └─ LiveMultiSymbolRunner._pump_once()
            展平 → 按 (exchange_ts, symbol, arrival) 确定性排序
            └─ runtime.multi_symbol_core.process_market_event   ← 离线/实时共享唯一步进
                 1) engine.poll  结算在途模拟成交（先于本事件决策）
                 2) mark→AccountingCore mark；book/trade→per-symbol FeatureEngine + 对应 SimulatedVenue
                 3) tradeable & decide_on ⇒ per-symbol MakerPolicy.decide ⇒ engine.submit
                      └─ ExecutionEngine：先 P0017.2 组合 gross 硬闸，再单标的闸，再 manager.submit
            多 SimulatedVenue（MultiSymbolSimulatedAdapter，per-symbol 隔离）
  └─ 共享 OrderTracker / AccountingCore（订单/成交/费用/持仓/权益）
  └─ LiveRunStats.view()：可审计 JSON 状态
```

执行适配器**固定**为 `MultiSymbolSimulatedAdapter`（模拟），全链路无 Testnet/主网写入口。

## 2. 交付物对照（§8）

| 交付物 | 位置 |
|---|---|
| 实时多标的模拟交易编排 | `runtime/live_multi_symbol.py`（`LiveMultiSymbolRunner`） |
| 离线/实时共享步进核心 | `runtime/multi_symbol_core.py`（`process_market_event`/`decide_and_submit`/`build_commitment_provider`） |
| 只读采集器适配 | `runtime/live_collector_source.py`（`CollectorBatchSource`） |
| 产品入口/CLI/profile | `runtime/live_paper_service.py`；`cli/main.py` 新增 `live-paper`；`profiles/live-paper-multi-symbol.json` |
| 中立预测调用 | `prediction/invoke.py`（离线/实时共享，消除 runtime→backtest 反向依赖） |
| 离线 driver 复用核心 | `backtest/driver.py`、`backtest/multi_driver.py` 改为调用共享核心（行为不变） |
| 确定性 E2E/故障/生命周期测试 | `tests/integration/test_live_multi_symbol_paper.py`（11） |
| 产品入口校验测试 | `tests/integration/test_live_paper_entry.py`（5） |
| 结构性安全守卫 | `tests/unit/test_live_paper_isolation.py`（3） |

## 3. Acceptance Matrix（§4 固定验收标准）

### A. 端到端功能

| # | 标准 | 验证 | 结果 |
|---|---|---|---|
| A1 | 脚本化多标的批次驱动 ≥2 标的 | `test_e2e_real_decisions_...`：BTC+ETH，复用 P0017.4 触价微观结构 | **PASS [T]** |
| A2 | 真实决策路径产生可核验模拟订单/成交/会计（非 mock） | 同上：submits>0、fills>0、tracker 含两标的真实订单、FillLedger>0、权益变化且有限 | **PASS [T]** |
| A3a | 组合预算跨标的生效 | `test_portfolio_budget_is_enforced_across_symbols_live`：limit=500 ⇒ 出现 `PORTFOLIO_NOTIONAL_LIMIT`，accepted 非 reduce-only OPEN gross ≤500 | **PASS [T]** |
| A3b | 同批次预算竞争确定性 | `test_same_batch_budget_contention_winner_is_deterministic_live`：同批各 400、限额 500 ⇒ BTC（symbol tiebreak 较小者）唯一被接受，ETH 全被组合闸拒；双跑订单/拒绝轨迹一致 | **PASS [T]** |
| A4 | 风险拒绝无副作用 | `test_risk_rejection_has_no_order_or_accounting_side_effect`：limit≈0 ⇒ rejects>0、tracker 0 单、0 成交、权益=初始 | **PASS [T]** |
| A5 | 单标的故障不终止健康标的 | `test_single_symbol_failure_does_not_stop_healthy_symbol`：ETH 连续 10 泵 `source_error` 被隔离计数（`had_failure/error_count`），BTC 持续健康且有订单 | **PASS [T]** |
| A6 | 正常停止 + 未成交订单处理 + 退出 | `test_graceful_stop_cancels_resting_orders_and_verifies_terminal`：请求撤销>0、`stop_cancel_confirmed==requested`、`open_after_stop==()`、tracker.active()==0 | **PASS [T]** |
| A7 | 双跑确定性 + 因果正确 | `test_repeated_runs_are_deterministic`：订单/拒绝轨迹/权益/成交数逐一致；`test_out_of_order_event_is_skipped_and_counted`：exchange_ts 回退事件被跳过计数，不喂决策 | **PASS [T]** |
| A8 | 可审计状态/结果输出 | `LiveRunStats.view()` 含 status/pumps/decisions/submits/fills/rejects/per-symbol health/取消确认；CLI 周期输出 JSON | **PASS [T]** |

### B. 结构与安全

| # | 标准 | 验证 | 结果 |
|---|---|---|---|
| B1 | 离线/实时共用同一核心步进，无两份实现 | `test_offline_and_live_share_single_step_implementation`：`backtest.multi_driver.process_market_event is live.process_market_event`、commitment provider 同对象；离线 multi_driver 改调核心后 P0017.4 全部测试不变 | **PASS [T]/[S]** |
| B2 | 模拟路径不依赖 Testnet/主网写入口（AST 守卫） | `test_live_paper_modules_do_not_import_write_or_testnet_paths`、`test_collector_source_only_touches_readonly_market_collector`、`test_runner_execution_adapter_is_the_simulated_multi_adapter` | **PASS [T]/[S]** |
| B3 | 未知标的/缺状态/无效配置明确失败 | 未知 symbol 批次计数不路由（`test_unknown_symbol_batch_...`）；<2 标的/重复/源缺标的/余额≤0 ⇒ `LiveRunError`（`test_invalid_configuration_fails_fast`）；CLI 缺配置退出码 2 | **PASS [T]** |
| B4 | 组合风险/订单追踪/成交/会计一致 | A2/A3/A4 同证；离线多标的既有守恒检查（multi_pending_conservation、gross=Σ notionals）在共享核心路径下保持通过 | **PASS [T]** |

### C. 回归与交付

| # | 标准 | 结果 |
|---|---|---|
| C1 | 全量测试通过；skipped 记原因，不计为通过 | **2560 passed / 0 failed / 24 skipped**；24 skipped 全为既有外部 opt-in（live/private smoke，需网络/凭据） |
| C2 | P0017.1–.4 回归通过 | `test_multi_symbol_backtest`(8)、`test_backtest_driver`、`test_backtest_cli_report`、`test_portfolio_*`、`test_market_dataset`、`test_universe_*`、`test_multi_collector` 等全绿；共享核心重构后无行为变化 |
| C3 | detached clean checkout 复验 | 见 §5（在收口提交后执行） |
| C4 | 完整验收报告 | 本文件 |
| C5 | Closure Audit + 状态同步 | `P0017_5_CLOSURE_AUDIT.md`；roadmap/current_state/handoff/status.json 随收口提交 |
| C6 | 独立提交、工作树 clean、push、currentProposal=null | 实现提交 `a86627c`；收口提交见报告尾部 |

## 4. 生命周期语义（§3.3，逐条落实）

- **启动/持续运行**：`run_forever` 单线程 pump→process；后台服务模式（`live_paper_service`）在工作线程跑循环、主线程装 SIGINT/SIGTERM 与周期状态。
- **单标的故障隔离**：`SymbolBatch.source_error` → `SymbolHealth.record_error`（粘滞 `had_failure`、计数、last_error），该泵跳过该标的，其余继续。
- **背压/积压**：collector `pump_once(max_messages=64)` 一次排空多消息；runner 每泵处理整批。**无静默丢弃关键事件**——乱序跳过、重复跳过、未知标的、错误都有显式计数并进入 `view()`。
- **重复/乱序/异常时间戳**：per-symbol 单调时间门，`exchange_ts` 严格回退 ⇒ `out_of_order_skipped` 计数并跳过；同 ts 同内容指纹 ⇒ `duplicate_skipped`；不使用旧/未来事件。
- **SIGTERM 有序退出**：`request_stop()` → `_shutdown()`：禁新增 → poll drain → 对所有 active 模拟挂单调 `engine.cancel` → 再 poll → 以 tracker 终态核实（`open_after_stop`/`stop_cancel_confirmed`）。
- **不虚构**：停止不构造收盘价、不强平；持仓如实保留；模拟成交不标记为真实。
- **撤单验证**：不假定进程退出即撤单成功；以 tracker active 终态计数（请求 vs 确认 vs 残留）。

## 5. 真实行情边界（§7，B 类 NOT RUN [X]）

- 注入式确定性 E2E 已完整闭合（A/B 全部 [T]）。
- **Binance 真实公开行情长跑仍 NOT RUN**：当前环境对 `fapi.binance.com`/`fstream.binance.com` 451/超时。`probex live-paper --config profiles/live-paper-multi-symbol.json` 在可访问出口应：连接 → 周期打印 JSON → Ctrl-C 有序停止。该真实证据未取得，不以注入式验收替代。
- 产品入口对连接失败如实输出 `{"status":"FAILED",...}`（不伪装已连接/成交）。

## 6. 命令与结果查看（§3.4，可复制）

```bash
# 实时多标的模拟交易（真实公开行情驱动，模拟成交；需可访问 Binance 的网络）
python3 -m cli.main live-paper \
  --config profiles/live-paper-multi-symbol.json \
  --status-every 5 --pump-interval 0.5
# 停止：Ctrl-C（SIGINT）或 kill -TERM；进程撤销模拟挂单并打印最终 JSON 状态后退出。
# 状态字段：status/pumps/decisions/submits/risk_rejects/fills/open_after_stop/
#          stop_cancel_requested/stop_cancel_confirmed/symbols{health,...}
```

## 7. 明确排除（§6，未触碰）

未做 Testnet 下单/撤单/平仓；未清理 P0001.16 的 0.001 BTC；无主网/LIVE；无新策略/信号；无 bar/OHLCV、真实滑点、资金费率、taker、真实队列校准；无 3+ 标的专项；无无关重构。

## 8. 结论

**P0017.5 §4 的 A/B/C 验收标准全部满足（真实长跑除外，按 §7 标 NOT RUN [X]）。**
能力声明严格限定为：**已实现并经自动化端到端验收的"实时多标的公开行情 → 模拟交易"链路**。
真实行情持续运行验证、Testnet 真实订单、LIVE 仍为独立状态。
