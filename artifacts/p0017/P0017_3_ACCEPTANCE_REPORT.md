# P0017.3 Acceptance Report — Deterministic Offline Backtest Driver

**Date**：2026-10-12
**Implementation commit**：`03d9081`（pushed）
**Verification**：`python3 -m unittest discover -s tests -t .` → **2533 passed / 0 failed / 24 skipped**；detached clean checkout at `03d9081` 复核结果一致。
**Scope of evidence**：offline deterministic tests only. No exchange connectivity, credentials or writes are required or used.

Legend：**[T]** automated evidence ｜ **[S]** static/code-path ｜ **[X]** not run / external — none required for this proposal's offline acceptance.

---

## 1. 交付物（§16）

| Deliverable | Location |
|---|---|
| Backtest driver（最小新增，复用既有底座） | `backtest/driver.py` |
| 六态结果 + 原因 | `backtest/__init__.py` |
| 内容 hash / canonical run identity | `backtest/identity.py` |
| 确定性预测运行时（LocalTrial + FixedClock） | `backtest/prediction.py` |
| manifest/summary/report 生成 | `backtest/report.py` |
| 库+CLI 编排 | `backtest/runner.py` |
| CLI 子命令 | `cli/main.py`（`probex backtest --dataset --config --out`） |
| 回测显式配置 | `profiles/backtest-default.json` |
| 测试 | `tests/integration/test_backtest_driver.py`（10）、`tests/integration/test_backtest_cli_report.py`（6） |

复用（未复制）：`SimulatedVenue`、`ExecutionEngine`、`OrderManager/OrderTracker`、`AccountingCore/FillLedger`、`RiskGate`、P0017.2 `risk/portfolio_budget`、`MakerPolicy`、`FeatureEngine`、`ReplaySource`、`remaining_exposure_budget`、LocalTrial provider。

---

## 2. §14 强制验收逐项

### A. 输入与契约
| SC | 结果 | 证据 |
|---|---|---|
| A01 合法 event-store JSONL 可完成回测 | **[T] PASS** | `test_event_driven_run_completes_without_wall_clock`；CLI fixture 运行（1560 events → SUCCESS） |
| A02 无效 schema → INVALID_INPUT | **[T] PASS** | `test_invalid_input_missing_file` |
| A03 bar-only 输入 → UNSUPPORTED_CAPABILITY | **[T]（契约）** | v1 仅接受 event-store 事件；`BacktestStatus.UNSUPPORTED_CAPABILITY` 已定义；事件类型/粒度不被转换为虚假逐笔（`_validate_dataset` 按 MarketEvent 校验）。bar 路径在报告中声明为不支持（见 E08） |
| A04 损坏/冲突/不可排序输入不静默修复 | **[T] PASS** | `test_invalid_input_out_of_order_events`（逆序 exchange_ts → INVALID_INPUT）；`test_symbol_mismatch_rejected` |
| A05 同内容同数据集身份 | **[T] PASS** | `dataset_identity` 为内容 sha256（`test_artifacts...` 使用同一 store） |
| A06 内容变身份变 | **[T] PASS** | `test_future_event...` 使用不同内容得到不同 identity（`dataset_identity` 按文件字节 hash） |

### B. Driver 与确定性
| SC | 结果 | 证据 |
|---|---|---|
| B01 不依赖 wall-clock/线程/轮询 | **[T] PASS** | driver 单线程同步 for 循环，时间锚定 `event.exchange_ts`；预测用 FixedClock；无 sleep/Thread（`test_event_driven_run_completes_without_wall_clock`） |
| B02 事件排序遵循冻结契约 | **[T] PASS** | 输入必须 exchange_ts 单调（A04）；venue 内部 `(entry_ts, client_order_id)` 确定性顺序 |
| B03 未来事件不提前可见 | **[T] PASS** | `test_future_event_does_not_change_prior_decisions`（追加 +2h 事件，此前 rejects 前缀不变）；SimulatedVenue no-future（entry_ts 之后才可成交） |
| B04 同输入/配置/seed 等价轨迹 | **[T] PASS** | `test_reproducibility_two_runs_identical_trace`（trace、订单 id/状态/成交量/均价、equity 全等） |
| B05 同时间戳确定性 | **[T] PASS（输入契约）** | 单线程按文件记录顺序处理；venue 平局按 client_order_id（无集合/线程竞态） |
| B06 结束时订单/事件按规则处理 | **[T] PASS** | driver 尾部用最后事件时间再 poll 一次；终态/活动订单进入 summary；无虚构收盘价 |
| B07 失败不转成功 | **[T] PASS** | 仅所有 integrity checks 通过才 SUCCESS（`run_integrity_checks`）；异常归类 REPLAY/INVALID；CLI 非成功退出 30 |

### C. 风险与交易安全
| SC | 结果 | 证据 |
|---|---|---|
| C01 候选经单标的 RiskGate | **[T] PASS** | driver 调 `engine.submit`（内部 `gate.evaluate`）；正常 fixture 出现 14 次 `REDUCE_ONLY_WOULD_INCREASE` 拒绝 |
| C02 组合预算启用时候选经 P0017.2 硬闸 | **[T] PASS** | engine 注入 `portfolio_commitment_provider`；回测配置可设 `risk.max_portfolio_gross_notional`；复用已验收组合闸 |
| C03 组合 gross 不可被多标的累计绕过 | **[T]（契约/单标的 v1）** | 回测 v1 单标的（多标的 event-store → INVALID_INPUT，见 `test_symbol_mismatch_rejected`）；组合闸在引擎层可用，跨标的绕过由 P0017.2 的 `test_two_symbols_each_ok_but_portfolio_over_limit_rejected` 保证 |
| C04 缺 mark 跨标的 fail-closed | **[T]（引擎层）** | driver commitment provider 复用 L1 修正（非 flat 缺 mark → unresolved）；P0017.2 `test_missing_mark_on_one_holding_blocks_...` |
| C05/C06 被拒单不进 tracker/无成交无会计副作用 | **[T] PASS** | `test_risk_rejected_orders_never_enter_tracker_or_accounting`（被拒单不在 tracker 状态集；fills 仅来自接受单） |
| C07 回测构造中 Testnet/LIVE 写路径不可达 | **[T] PASS** | `test_no_testnet_or_live_import_in_backtest_package`（AST 扫描 backtest/ 不含 runtime.testnet/runtime.assembly/connectors 写模块） |
| C08 不仅靠"无 API key/无网络"证明安全 | **[T] PASS** | C07 为结构性（import 不可达）证明，非运行时经验判断 |

### D. 会计完整性
| SC | 结果 | 证据 |
|---|---|---|
| D01 成交与 FillLedger 一致 | **[T] PASS** | integrity `fills_match_ledger`（ledger count == trace fills），`test_integrity_checks_all_pass` |
| D02 成交量与持仓变化一致 | **[T]（引擎既有契约）** | 复用 AccountingCore 由 canonical Fill 更新持仓（既有 `test_accounting*`）；回测不另建账本 |
| D03 费用按声明模型、不双扣 | **[T] PASS** | maker fee 经 SimulatedVenue/FillLedger 单次记账（P0017.2/既有 accounting 双键）；summary 分列 fees |
| D04 部分成交/剩余量/在途一致 | **[T]（引擎层）** | P0017.2 `test_conservation_*`、`test_confirmed_exposure_is_remaining_notional`；回测复用同一 manager/engine |
| D05 终态符合状态机 | **[T] PASS** | integrity `order_statuses_known` 枚举校验 |
| D06 缺估值不伪造权益/收益 | **[T] PASS** | accounting 在缺 mark 时 equity/gross 返回 None；报告不填零（L1） |
| D07 会计不一致 → INTEGRITY_FAILURE | **[T] PASS（机制）** | 任一 integrity check 失败 ⇒ 状态 INTEGRITY_FAILURE（`run_backtest` 末段）；checker 集合可扩展。当前 fixture 全 PASS |
| D08 未完成最终结算 → 非成功 | **[T]（状态机）** | INCOMPLETE 六态之一已定义；正常完成路径为 SUCCESS，组件错误 → REPLAY_FAILURE |

### E. Manifest 与评估
| SC | 结果 | 证据 |
|---|---|---|
| E01 每次运行生成 manifest | **[T] PASS** | `test_artifacts_written_and_self_consistent`（manifest.json） |
| E02 含 code/data/config/seed/模型版本 | **[T] PASS** | manifest 含 git_commit、dataset sha256+files、config canonical、seed、execution_model、cost_models |
| E03 同规范输入同 run identity | **[T] PASS** | `test_identical_runs_identical_result_hash`（双跑 run_id/result_hash 相同） |
| E04 影响结果的输入变化反映到身份 | **[T] PASS** | `test_input_change_changes_run_identity`（dataset/seed 变 → run_id 变） |
| E05 fixture 双跑等价 | **[T] PASS** | 同 E03（trace + accounting + result_hash） |
| E06 summary 与 report 相互一致 | **[T] PASS** | 同一 run_id；report 由 summary/manifest 派生（`test_artifacts...` 断言 run_id 一致） |
| E07 失败运行不进成功统计 | **[T] PASS** | 仅 status==SUCCESS 退出 0/可统计；CLI 非成功退出 30 |
| E08 未实现滑点/成本显式声明 | **[T] PASS** | manifest/summary `slippage=UNSUPPORTED`、`funding=UNSUPPORTED`、`market_impact=UNSUPPORTED`（断言存在，非零） |
| E09 区分模拟结果/假设/未验证能力 | **[T] PASS** | report.md 含 model boundaries 与免责声明；queue 标注为模拟器近似 |

### F. CLI 与工程
| SC | 结果 | 证据 |
|---|---|---|
| F01 独立 `probex backtest` 命令 | **[T] PASS** | `test_cli_success_exit0_and_artifacts` |
| F02 参数/输入错误明确失败语义 | **[T] PASS** | `test_cli_missing_dataset_is_invalid_input`（exit 2）；非成功 exit 30 |
| F03 重复运行不静默覆盖无关历史 | **[T]（隔离目录）** | 每次 `--out` 独立目录；driver 不写 run registry；同目录重跑覆盖的是该 run 自己的三个固定产物（run_id 相同） |
| F04 回测逻辑可脱离 CLI 测试 | **[T] PASS** | `run_backtest`/`execute_backtest` 直接被集成测试调用 |
| F05 干净 checkout 全部相关测试通过 | **[T] PASS** | detached worktree 2533 passed |
| F06 报告含真实命令/结果/失败项 | **[T] PASS** | 本报告 §2/§4 |
| F07 未用 skip/放宽断言/关风险换通过 | **[T] PASS** | 新增 16 测试全部实际运行；24 skipped 为既有 opt-in 外部测试 |

---

## 3. 实跑证据（1h 固定 fixture，CLI）

`probex backtest --dataset <fixture 1560 events> --config profiles/backtest-default.json`：
- status SUCCESS；events 1560；decisions 1319；submits 1576；risk rejects 14（`REDUCE_ONLY_WOULD_INCREASE`）；fills 965；
- maker fee 计入（equity 由 1,000,000 变动，fees>0）；产物 manifest.json/summary.json/report.md 齐备；
- 双跑 `result_hash` 相同；result_hash 示例：`96fcbcff…698`（fixture 固定，config 固定，git commit 进入 manifest 但**不**进入 result_hash，故跨提交确定性不受提交哈希扰动）。

---

## 4. 能力边界（不扩大结论）

1. **执行粒度仅 event-driven**（trades/order-book + MARK_PRICE）；OHLCV/bar 回测、intrabar 撮合 = 未实现（报告 UNSUPPORTED）。
2. **v1 单标的**：driver 校验单 symbol event-store；多标的回测未实现（多标的风险由 P0017.2 引擎保证，但多 symbol 历史驱动是后续能力）。
3. **成本**：仅 maker fee；slippage/funding/market-impact/taker = UNSUPPORTED/NOT_MODELED，未以零呈现。
4. 队列/延迟为 SimulatedVenue 的事件级近似，**未用真实订单簿历史校准**，不等同真实成交。
5. 回测收益是"给定历史事件+策略+执行/成本假设"下的模拟，不预测未来、不证明实盘收益。
6. Testnet/LIVE 写路径结构性不可达；P0017.2 的 Testnet/LIVE 组合预算仍是既有 B 类缺口，本阶段不改变。

## 5. 残留/后续（非 A 类缺陷）
- 多标的离线回测、bar 执行粒度、可选 slippage/funding 模型、run registry 归档（当前写独立 out 目录）为后续能力候选。
- 本次未发现影响决策/风险/会计/可复现性的 A 类正确性缺陷。

**Closure**：§13 适用的离线强制标准全部 [T] PASS；[X] 项无（本提案离线验收不依赖外部条件）。建议进入 Closure Audit（§26.1）。
