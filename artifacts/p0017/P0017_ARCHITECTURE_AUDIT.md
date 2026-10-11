# P0017 只读架构审计：多标的市场数据平台 + 多执行模式

**审计日期**：2026-10-11
**状态**：只读；未改任何代码；`currentProposal = null`
**结论口径**：区分「✅能力已存在（有证据）」「🧩仅底层组件（无产品入口/未接线）」「⛔尚未实现」

---

## 0. 四层架构 × 当前代码事实

| 层 | 角色 | 现状 |
|---|---|---|
| L1 市场数据平台 | 发现/采集/历史/统一时间轴/质量/持久化 | 🧩 单标的真实采集可用（`--market-source binance-public`）；**发现/多标的采集/独立数据服务**未实现 |
| L2 策略与决策 | 读行情产出可审计意图，与执行解耦 | ✅ `MakerPolicy`→`MakerDecision` 不持有 adapter（已解耦）；但**单标的上下文**（`QuoteLifecycle(symbol=...)`） |
| L3 执行适配层 | 统一契约，分别实现 回测/模拟/Paper/真实 | 🧩 `ExecutionAdapter` 契约存在；Paper + SimulatedVenue + Binance adapter 已有；**Backtest adapter 无**；各模式无统一"执行模式注册表" |
| L4 评估与产品界面 | 统一展示决策/证据/PnL/风险/可复现报告 | 🧩 RunSummary + 指标定义 + UI 已有；**回测报告/参数化历史运行/可复现实验工具链无** |

---

## 1. L1 市场数据平台

### 已存在 ✅
- 统一事件契约：`MarketEvent(exchange_ts/receive_ts/process_ts)`（`market/events/types.py`），三时间戳贯穿。
- 真实公开采集：`LiveMarketDataRuntime`（REST snapshot + WS depth/aggTrade/markPrice + resync/重连/telemetry；无需凭据）。
- 单标的产品接线：`runtime/assembly.py::_build_binance_public_source` + `PublicMarketPump` + `MarketSourceAdapter` 协议（`venue/market_source.py`）——**扩展点已就位**，新数据源只需产出 `MarketState`。
- 持久化：`RunRegistry.append_run_facts(kind in market/facts/trades)`（`storage/run_registry.py`），历史 run 可读、图表可定位。
- 合流能力：`combined_stream_url(streams)`（一条 WS 可订阅多 stream）。

### 仅组件 / 未接线 🧩
- **Universe 发现**：`exchangeInfo` 客户端存在（`GET /fapi/v1/exchangeInfo`，返回全市场 `symbols`），但只用于取**当前单标的** `TradingRules`；没有"发现可交易对 + 状态筛选（TRADING）+ 用户选择 + 订阅上限"的产品层。
- **多标的采集**：`LiveMarketDataRuntime` / `FeatureEngine` 构造即绑定**一个** symbol（`config.symbol`；`FeatureEngine(venue, symbol)`）。没有"一个连接合流多 symbol、按 symbol 分流到隔离状态"的运行器。
- **数据作为独立服务**：当前数据只在"一次产品运行"内存在（绑定 run_id 的有界缓冲 + run facts）；没有"不依赖交易/账户、可独立查询的行情数据集"（D-A）。

### 关键结论
- WS 合流 + 三时间戳 + 适配器协议使多标的**技术可行且不破坏契约**；工作量在"发现→订阅编排→按 symbol 隔离的状态/持久化/健康"这一新产品层，而非重写连接器。
- Binance USDⓈ-M 连接 stream 数有官方上限（需实现前核实当前配额；代码注释已标注端点存在第三方不确定性），必须做订阅上限/合流，不能无界订阅全部市场（提案已要求）。

---

## 2. L2 策略与决策

- ✅ `MakerDecision`（`strategy/maker/types.py:340`）纯数据，不含 adapter；决策→执行经 `ExecutionEngine`，解耦成立。
- 🧩 `QuoteLifecycle`/`MakerPolicy` 每次调用按 `symbol` 产生 `OrderProposal(symbol=...)`，但运行编排（`RuntimeDecisionLoop`）只持有**一个** state provider、一个 symbol 上下文。多标的需要"每 symbol 一个 market state + 一个策略上下文"，共享同一 decision→risk→execution 管道。
- ⛔ 无"决策意图与执行结果可分别回放"的显式多消费端编排（回测/实盘共用同一 decide 函数的 runner 抽象不存在）。

---

## 3. L3 执行适配层（四种输出）

| 输出 | Adapter | 状态 |
|---|---|---|
| 真实交易 | `connectors/binance/execution/adapter.py` + `BinancePrivateExecutionConnector` | ✅ 代码在（写路径受 readiness authority 门控）；真实端到端 = P0001.16 未收口（**写授权问题，非本提案**） |
| Paper（实时行情+模拟执行） | `connectors/paper/execution.py`（PaperExecutionConnector）+ `PaperBroker` | ✅ 已接线 |
| 模拟成交（事件级回放） | `execution/simulation/venue.py::SimulatedVenue`（队列/费/延迟/no-future） | ✅ 组件完整；已能在 PAPER 运行里驱动 |
| **回测（历史数据+撮合+指标报告+可复现）** | — | ⛔ **无 backtest adapter、无用户入口、无报告产物**（`grep backtest` 在 cli/reports/runtime 为空） |

**契约评估**：`ExecutionAdapter`（submit/cancel/poll/open_orders/recent_fills）是同步、面向"在线"的；回测是"离线、确定、按历史时间推进"，强行套同一 Protocol 会扭曲语义。**建议**：定义一个更高层的统一 `ExecutionMode` 概念，回测复用 `SimulatedVenue` 的撮合内核 + 独立的离线 driver，而非让回测实现 `poll()`。

---

## 4. L4 评估与产品界面

- ✅ `reports/metrics.py`：sharpe / mdd / profit_factor / resample_equity 等定义与计算（UNKNOWN 语义、最小样本）；RunSummary JSON+Markdown 确定性。
- 🧩 指标是"单次 run"维度；回测需要的"参数/区间/假设 + 逐笔成交 PnL + 手续费/滑点敏感性 + 未来泄漏检查 + 可复现指纹"**无**。
- ⛔ UI 无"回测任务/结果对比"概念；全标的列表页无（单标的）。

---

## 5. 权威 Owner 多标的就绪度（关键安全结论）

| Owner | 是否已天然支持多 symbol | 证据 |
|---|---|---|
| `AccountingCore` | ✅ 基本就绪 | `_positions: dict[str,Position]`、`_marks: dict[str,float]`、`position(symbol)`、按 symbol mark |
| `FillLedger` | ✅ 就绪 | 双键 `(venue,symbol,fill_id)`；`for_symbol(symbol)` |
| `OrderTracker` | ✅ 数据就绪 / 🧩 缺索引 | orders 是扁平列表，`Order.symbol` 存在，但无 `orders_for_symbol`（只有 `orders_for_decision`）；**仍是唯一 Owner，不需要第二个** |
| `RiskGate` | 🧩 **缺组合级预算** | `RiskSnapshot` 是**单 symbol**（`risk/snapshot.py`），gate 校验 `proposal.symbol == snapshot.symbol`。多标的需要：per-symbol 快照 + **组合级合计预算**（否则每 symbol 单独过限、合计超限——提案明确点名） |
| `OrderProposal/Order` | ✅ 带 symbol + correlation | 不串标的有数据基础；需运行编排保证 correlation 正确 |

---

## 6. 对 P0017 草案的修正建议（给人类决策）

1. **拆成"总提案 + 子阶段"是对的**，且第一层不应假设交易。建议编号：
   - **P0017.1 多标的市场数据平台**（发现/采集/隔离持久化/健康/全标的 UI + 数据独立于交易）
   - **P0017.2 多标的 PAPER 运行**（每 symbol 策略上下文 + **组合级风险预算** + 单一 tracker/accounting/gate + 不串标/不重复记账测试）
   - **P0017.3 回测执行模式与评估报告**（离线 driver 复用 SimulatedVenue；可复现；手续费/滑点/未来泄漏验证）——草案把它隐含在"四输出"里，应显式立项
   - TESTNET/真实多标的：独立提案 + 写授权（不在本提案授权内）
2. **P0017.1 的最大新增面是"数据平台"而非连接器**：不要重写 Binance client；新增 Universe 发现 + 多 symbol 订阅编排（合流、上限）+ 按 symbol 隔离的状态/持久化/健康 + 只读数据查询。
3. **组合级风险是 P0017.2 的核心新领域规则**，属"新增预算约束"——需要人类在提案中明确其语义来源（是否复用 `RiskLimits` 的组合字段 vs 新增），不能由实现自行发明数值。
4. **数据真实性前置**：当前出口对 Binance 451。P0017.1 的真实采集验收依赖一个非受限出口；离线可验证编排/解析/隔离，真实 E2E 需网络条件（诚实 NOT RUN，不伪造）。
5. 草案中的"DEMO 模式"当前对应 `scripts/probex.sh demo`（合成）——需在提案里把 DEMO 定义为**显式标记、永不进入正式产品状态**的独立模式，与 PAPER（真实行情）严格分离。

**下一步（待批准）**：人类确认四层拆分与编号后，我先提交 **P0017.1 最终可批准版本**（含逐文件影响、状态契约、订阅上限核实计划、成功标准与真实/离线双验收），再实施。
