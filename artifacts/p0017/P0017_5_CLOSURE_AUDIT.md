# P0017.5 Closure Audit — 实时多标的模拟交易收口

**日期**：2026-10-12　**Proposal**：P0017.5　**方法**：§26.1 五问 + A/B/C 分类

---

## 0. 结论（先行）

**建议收口。** 无 A 类正确性/安全缺陷；提案 §4 的 A/B/C 全部满足（真实 Binance 长跑为 B 类外部 NOT RUN，不计 PASS）。
本次交付是系统终态闭环审计中的**唯一核心连接层 G-1**；收口后，L0/L1（代码）/L2 三条非实盘链路均已闭合，剩余仅为外部验证（L1/L2 真实长跑）与需独立授权的 L3，不存在新的"连接层"。

测试：**2560 passed / 0 failed / 24 skipped**（工作树 + detached clean checkout @ `a86627c` 两次一致）。

## 1. 五问

### Q1 承诺是否闭合（promise closure）
- 产品目标链路（行情→状态→决策→组合风险→多模拟 venue→会计→可审计输出）端到端打通并有真实（非 mock）订单/成交/权益证据（A1/A2/A8 [T]）。
- §3.1–§3.4、§8 交付物逐项落地（见验收报告 §2）。
- §3.4 产品入口：CLI `live-paper` + profile + 启动/停止/状态查看说明齐备；配置在触网前校验。

### Q2 正确性是否闭合（correctness closure）
- 组合 gross 硬闸在实时路径跨标的生效，且同批次竞争确定性（A3a/A3b [T]，复用 P0017.2 已裁决口径；reduce-only 语义未改动）。
- 风险拒绝无 manager 订单/成交/会计副作用（A4 [T]）。
- 会计勾稽：FillLedger/权益/持仓随真实模拟成交变化且有限；离线多标的守恒检查在共享核心下保持通过（B4 [T]）。
- 因果：per-symbol 单调时间门，乱序跳过计数、重复跳过，不用未来/旧事件（A7 [T]）。
- 撤单验证：停止以 tracker 终态核实，不假定撤销成功（A6 [T]）。

### Q3 能力边界是否如实（capability boundary）
- 声明严格限定："**已实现并经自动化 E2E 验收的实时多标的公开行情→模拟交易**"。
- 模拟 ≠ 真实：执行适配器结构上固定为 `MultiSymbolSimulatedAdapter`，AST 守卫确认无 `runtime.testnet`/binance 写/private 入口（B2 [S]）。不存在配置/注入错误降级为真实下单的路径。
- 真实 Binance 长跑 = **NOT RUN [X]**（451/超时）；Testnet/LIVE 未触及。

### Q4 遗留处置（leftover disposition）
见 §2 分类表。无 A 类。

### Q5 状态一致性（state consistency）
- 收口提交：currentProposal→null、roadmap/current_state/handoff 更新；与本审计、验收报告一致。
- 设计正文 `proposals/P0017.5-...md` 收口后状态改为已关闭（离线/注入式 E2E 口径）。

## 2. 遗留分类

| ID | 项 | 类别 | 处置 | 解除条件 |
|---|---|---|---|---|
| B-live | 真实 Binance 公开行情长跑（连接/持续/重连的**真实证据**） | **B（外部环境）** | 代码与注入式 E2E 已闭合；NOT RUN 保留 | 具备非 451 出口，跑 `live-paper` 取得真实 JSON 状态/落盘证据 |
| B-L3 | Testnet 真实订单（P0001.16 SC-11/12/13/6/7/15） | **B（独立授权）** | 不在本提案；底座大部分已在 | 单独授权 Testnet 写 + 凭据 + 出口 |
| B-residue | P0001.16 残留 0.001 BTC | **B（授权平仓）** | 明确排除 | Testnet 授权后随 L3 收尾 |
| B-L3budget | Testnet engine 未接组合预算 provider | **B（随 L3 一行接线）** | 不在本提案 | L3 阶段 |
| C-backpress | 真实背压/积压的量化水位与告警阈值 | **C（可观察性增强）** | 已有 max_messages 排空 + 无静默丢弃计数；阈值调优为后续 | 真实长跑取得数据后 |
| C-3sym | 3+ 标的专项 | **C** | 当前 ≥2 满足设计 | 独立增强 |
| C-cost | bar/OHLCV、真实滑点、funding、taker、队列校准 | **C/明确排除** | 不阻塞 | 独立提案 |

**A 类：无。** 分类原则：真实正确性缺陷不得降级为 C；外部/授权项不伪装成已验证。

## 3. 关键设计/实现裁决（可追溯）

1. **共享唯一步进（B1）**：新增 `runtime/multi_symbol_core.py`，离线 multi_driver 与实时 runner 都调用 `process_market_event`；测试断言两路径引用**同一函数对象**。这是"不再维护两套决策/执行"的结构性保证，而非口头约定。
2. **依赖方向修正**：预测同步调用抽到中立的 `prediction/invoke.py`，消除 `runtime→backtest` 反向依赖；离线 `_predict` 委托之（行为不变，离线 2540 测试在重构后保持全绿）。
3. **确定性顺序**：实时单线程 pump→process；批内 `(exchange_ts, symbol, arrival)` 排序（跨标的 symbol 作为确定性 tiebreak，离线则用 store ordinal）。同批次预算只够一个时 symbol 较小者确定性胜出（A3b）。
4. **生命周期安全失败**：未知标的/乱序/重复/采集错误全部计数可观察、不静默丢弃、不杀全局；停止只撤销模拟挂单并核实终态，不强平、不虚构收盘价。
5. **产品入口 fail-fast**：标的/风险/策略配置在建立任何网络连接前校验；连接失败如实 FAILED 退出。

## 4. 回归证据

- 新增 18 测试（live E2E 11 + 入口 5 + 隔离守卫 3，另有 1 个既有 ownership 测试更新为承认第二个显式组合根）。
- 全量 **2560 passed / 0 failed / 24 skipped**；24 skipped 均为既有外部 opt-in，无一本提案路径。
- detached clean checkout @ `a86627c` 复验同数，通过。
- 对既有测试的唯一修改：`test_exactly_one_paper_broker_owns_execution` 允许新增的显式组合根 `live_multi_symbol.py`（多模拟 venue），单标的产品装配仍唯一在 `assembly.py`——这是 ownership 的受控扩展，非放松（已在测试注释说明）。

## 5. 收口动作清单（授权范围内）

- [x] 实现 + 测试 + 全量/回归 + detached 复验
- [x] 验收报告（`P0017_5_ACCEPTANCE_REPORT.md`）
- [ ] 本 Closure Audit 随收口提交
- [ ] currentProposal→null；roadmap 行改"已完成（注入式 E2E；真实长跑 B 类 NOT RUN）"；current_state/handoff
- [ ] 设计正文状态→已关闭；工作树 clean；push
- [ ] **不**创建 P0017.6，**不**触碰 Testnet/LIVE/0.001 残留

## 6. 最终判定

**P0017.5 可正式关闭。** 关闭后系统在 L0（离线单/多回测）与 L2（实时多标的公开行情→模拟交易）均为"已实现 + 自动化 E2E 验证"；L1 与 L2 的**真实外部长跑**、L3 Testnet、L4 LIVE 是明确登记的外部/授权事项，而非新发现的工程连接层。
