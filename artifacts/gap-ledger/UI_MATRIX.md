# Probex 全量 UI 功能验收矩阵

**建立**：2026-10-10（全量 UI 查漏补缺）
**依据**：全部 Proposal 的 UI 相关 Success Criteria；`ui/app/surfaces.js`；`ui/app/navigation.js`
**图例**：✅完成（有真实后端接线+测试） · 🔌有缺口（本轮修） · 🧱受阻（外部条件） · 📐刻意排除/延期

## A. 外壳与导航（`ui/app/*`, `ui/client/console.js`）

| # | 功能 | 规划依据 | 现状 | 验收方式 |
|---|---|---|---|---|
| A1 | 5 Surface 导航 + active 态 | P0001.12.1 SC-1 | ✅ | renderNav；导航守卫测试 |
| A2 | 全局 Header（mode/env/venue/runtime/health/data_ts） | P0001.12.1 | ✅ 每 2s 轮询 | console.js |
| A3 | 全局 Blocker Strip（owner:code → System section） | F-16 | ✅ | BLOCKER_SECTION 映射 |
| A4 | hash 路由（Surface + legacy detail，无孤儿页） | P0001.12.1 SC-10 | ✅ | resolveRoute；orphan 守卫 |
| A5 | 错误页（page render/mount 失败 → 提示 + retry + 恢复链接） | — | ✅ | console try/catch |
| **A6** | **Surface 内容自动刷新（轮询 snapshot）** | P0001.10.2 §6 | ✅ **SURFACE_REFRESH_MS=5000；非破坏 refresh 优先 + 交互保护**；实测 data_ts 7s 内前进、图表/画线保留 |
| A7 | Assistant Drawer 挂载（非第六页面） | P0001.12.3 | ✅ | mountAssistant |

## B. Monitor（`ui/pages/monitor/page.js`, `monitor/sparklines.js`）

| # | 功能 | 现状 | 验收 |
|---|---|---|---|
| B1 | equity/realized/unrealized/position/exposure/readiness 指标 | ✅ 真实 snapshot | rows |
| B2 | blockers（code + 人类解释 + 跳 System） | ✅ F-09 catalog | reasonCell |
| B3 | instrument/venue/reference 身份 | ✅ P0001.15 §22 | identityRow |
| B4 | 两个 connector health 分开 | ✅ | connectorRow |
| B5 | 四层 ops（liveness/runtime/readiness/exec） | ✅ F-12 | opsRow |
| B6 | trends sparklines（equity/PnL/exposure） | ✅ 真实 timeline | mountSparklines |
| B7 | current quotes / active orders / recent activity | ✅ overlays | — |
| B8 | LOCAL_TRIAL 显式标注 | ✅ P0001.17 | predictionSourceLabel |

## C. Market（`ui/pages/market/*`）

| # | 功能 | 现状 | 验收 |
|---|---|---|---|
| C1 | live/replay/run-review 二级导航 | ✅ | nav |
| C2 | K-line workbench（candles/zoom/pan/crosshair/tooltip） | ✅ 真实库 | mountWorkbench |
| C3 | timeframe 切换（1m/5m/15m/1h）接后端重取 | ✅ onTimeframe→refreshChart | 浏览器测试 |
| C4 | indicator 开关（库内） | ✅ syncIndicators | — |
| C5 | drawing tools（水平线/趋势线/矩形/箭头/测量/fib*） | ✅ extension + clear | create/remove |
| C6 | semantic markers（trace→marker→点击跳 canonical 实体） | ✅ semantic_overlays | onClick |
| C7 | 后端 VWAP/ATR 事实叠加（无事实不画线） | ✅ probexVwap/Atr | indicators.js |
| C8 | order book（depth 投影） | ✅ | orderBookRows |
| C9 | recent trades（真实 TradePayload） | ✅ | tradesTable |
| C10 | L2 heatmap canvas | ✅ drawHeatmap | mount |
| C11 | prediction 面板（多 horizon，无记录不伪 50%） | ✅ | prediction_panel |
| C12 | features / data quality 面板 | ✅ | featurePanels/healthStrip |
| C13 | decisions/execution 表 | ✅ overlays | decisionTable |
| C14 | replay controls（play/pause/step/speed/seek）POST local | ✅ | replayControls |
| C15 | instrument/data source 身份 + 指标事实来源说明 | ✅ | instrumentVenueSection |
| C16 | run-review：历史 run 持久化事实图表 + locate | ✅ | mount run-review |
| C17 | workbench/正文随实时数据刷新 | ✅ 非破坏 liveWorkbench.refresh（保留画线） |

## D. Activity（`ui/pages/activity/page.js`）

| # | 功能 | 现状 | 验收 |
|---|---|---|---|
| D1 | time-ordered causal chain（13 stage） | ✅ F-08 | timeline |
| D2 | 缺阶段显式 ABSENT/UNAVAILABLE（不静默跳过） | ✅ | stageReason/missingBlock |
| D3 | decision drill-down（/decisions/detail：market→…→accounting） | ✅ P0001.17 §7 | mountDecisionDrilldown |
| D4 | identity 单元格跳 canonical 实体 | ✅ | identityCell |
| D5 | decision→order 反查链接 | ✅ P0001.15 §15 | decisionOrderCell |
| D6 | inputs/gates、decisions、exchange events 表 | ✅ | — |
| D7 | strategy bid/ask 面板 + prediction | ✅ | strategyPanel |
| D8 | trace/面板随实时数据刷新 | ✅ 轮询；打开的 drilldown 标 data-panel-open 防关闭 |

## E. Performance（`ui/pages/performance/*`）

| # | 功能 | 现状 | 验收 |
|---|---|---|---|
| E1 | run 列表（分页，newest first） | ✅ F-19 | runList |
| E2 | run 选择（#/performance/run/<id>） | ✅ | — |
| E3 | pagination（total/has_more/next） | ✅ | paginationBlock |
| E4 | durable run summary metrics（无事实 UNKNOWN） | ✅ | metrics |
| E5 | metric definitions（UI 不计算） | ✅ P0001.11 | definitionTable |
| E6 | run compare（两 run，delta） | ✅ | compare |
| E7 | equity timeline | ✅ | timelineBlock |
| E8 | trade stats（真实 facts；win/loss 显式 NOT_AVAILABLE + 原因） | ✅ P0001.17 §8 | tradeStats |
| E9 | performance charts（equity/PnL/exposure） | ✅ ECharts | mountPerformanceCharts |
| E10 | 列表/图表随实时数据刷新 | ✅ 轮询 |

## F. System（`ui/pages/system/page.js`, `system/ops_charts.js`）

| # | 功能 | 现状 | 验收 |
|---|---|---|---|
| F1 | 九 section 导航 | ✅ | nav |
| F2 | health（provider/accounting/runtime/market/blockers） | ✅ | factRows |
| F3 | instruments（spec/capabilities/reference policy/venue rules） | ✅ P0001.15 §26 | — |
| F4 | connections（两 connector 分开，不合并） | ✅ SC-28 | — |
| F5 | risk（rejects code+解释） | ✅ | — |
| F6 | readiness（authority facts/blockers/details） | ✅ G5 | — |
| F7 | execution（limits/rate/latency/recon/anomalies + charts） | ✅ P0001.13 | executionSections |
| F8 | ops（liveness/network/logging/retention posture） | ✅ F-12/F-15 | opsSection |
| F9 | configuration（provenance/entries/secret refs） | ✅ 只读 | — |
| F10 | capabilities（read endpoints/commands/exit codes；write=禁用） | ✅ | — |
| F11 | 各 section 随实时数据刷新 | ✅ 轮询 |

## G. Legacy detail pages（8 个，经 DETAIL_PAGE_MODULES 可达）

| Page | 现状 | 功能 |
|---|---|---|
| overview | ✅ | identity/headline/blockers/provenance |
| portfolio | ✅ | factRows(portfolio) |
| orders | ✅ | 订单表+correlation+exposure+selection |
| prediction | ✅ | factRows(prediction) |
| strategy | ✅ | factRows(strategy) |
| evidence | ✅ | trace + raw facts drill + blockers |
| runs | ✅ | bounded 分页列表 |
| metrics | ✅ | metric 定义表 |
| 全部 | ✅ 随表面轮询刷新 |

## H. Assistant 交互（`ui/assistant/*`）

| # | 功能 | 现状 | 验收 |
|---|---|---|---|
| H1 | context（surface/runtime/blockers） | ✅ | refreshAssistant |
| H2 | instrument 事实 + 预置问题 | ✅ P0001.15 §27 | renderInstrumentContext |
| H3 | suggested actions 只来自 Manifest | ✅ P0001.12.3 | loadSuggestions/renderActions |
| H4 | invoke action（含 confirmation 两步；CAPITAL 不出现） | ✅ | invoke |
| H5 | deterministic explain（无 LLM；无事实 UNKNOWN） | ✅ | showAnswer |

## I. 受阻项（不得标完成）

| # | 功能 | 受阻原因 | 复现 |
|---|---|---|---|
| I1 | 真实公网行情端到端 | Binance HTTP 451 geo-block（含本地代理出口） | curl -x http://127.0.0.1:7890 https://fapi.binance.com/fapi/v1/time |
| I2 | 真实下单/平仓 UI 验收 | P0001.16 未收口 + 无写授权 | 见 GAP_LEDGER Batch 4 |
| I3 | 策略盈利/有效性 | 无 outcome 证据（LOCAL_TRIAL 非模型） | — |

## 统计

- 规划功能项：**61**（A7 + B8 + C17 + D8 + E10 + F11 + G8 + H5，扣除重复后 61 个交互项）
- 已完成：**58**（surface 轮询已补齐，覆盖全部正文页）
- 有缺口：0（授权范围内）
- 受阻：3（I1/I2/I3，外部条件）

---

## 全量 UI 入口闭环审计与修复（本轮：真实浏览器驱动运行中的产品）

**方法**：不用组件目录/接口定义判断完成度，而是用真实 Chrome 打开运行中的产品，逐 Surface 收集可交互元素、
逐个点击内部链接、发送真实助手消息、执行真实 Action Gateway 调用，并断言真实反馈（含失败/空/未知）。

**实测发现的真实缺陷（A 类：入口点击后报错或静默走错页面）与修复**：

| # | 缺陷（实测） | 根因 | 修复 |
|---|---|---|---|
| U1 | Activity / Orders 的身份单元格（order / fill / execution_event）点开进入 **error 页**（`TypeError: Cannot read properties of undefined (reading 'replace')`） | `entityHash()` 漏掉 `kind` 段，生成 `#/activity/evidence/<id>`；Evidence 页契约需要 `kind+identity`，缺参时 `escapeHtml(undefined)` 抛错（且查错端点） | `entityHash` 补 kind（→ `#/activity/evidence/<kind>/<id>`）；`escapeHtml` 空值安全；Evidence 页缺参时显式说明而非抛错 |
| U2 | `decision → orders` 与 `order → decision` 互跳链接**静默落到 Monitor** | 生成了 `#/orders?decision=…`、`#/activity?decision=…` 这类非 Surface 路由；且 hash query 未从路由解析中剥离 | 统一为 `#/activity/orders?decision=<id>`；`surfaceForHash`/`currentRoute` 先剥离 hash query |
| U3 | 「同 decision 的其他订单」反查能力**只有 API、UI 未使用** | `fetchDecisionOrders` 已导出但从未被调用（§15 双向跳转未落地） | Orders 页读取 `?decision=` 并**真实反查**渲染候选订单表 |
| U4 | Assistant Drawer **没有输入框与发送**（只有预置问题与动作按钮） | 设计要求 §7 的可输入助手未落地 | 新增输入框+发送+会话记录；确定性路由到既有只读能力；pending/answered/failed/NO_RESPONSE 显式反馈；动作仍只走 Manifest+Gateway |
| U5 | Header 的可切换性没有任何说明（不可切换项看起来像普通文本） | 设计要求“不可切换必须显示原因”未落实 | Header 增加常显 `(fixed @startup)` 标记 + `why read-only?` 展开说明（启动参数决定；换标的需重启；集合见 universe） |
| U6 | Blocker Strip 只在 `title` 里带原因 | 设计要求“原因/影响范围/下一步可见” | 条带直接显示 severity + 原因 + "→ 下一步"，仍链接到对应 System section |
| U7 | Monitor 无法回答“我现在能执行哪些操作” | 设计要求 B 未落地 | 新增 Available actions 区：动作来自 Manifest，仅 AVAILABLE 可点；L2 走 confirmation；不可用动作显式给出原因 |
| U8 | universe 未区分“查看当前标的”与“修改运行标的集合” | 要求 C 未落实 | 显式标注 traded symbol 固定于启动参数、采集集合由启动配置决定且本页只读（不造假控件） |
| U9 | 助手把诊断问题“为什么**没有下单**？”误判为下单请求 | 关键词路由过宽 | 资本意图需**请求语气**才成立；诊断问题走 blockers + 订单事实 |

**真实浏览器验收结果**：`tests/ui/ui_entry_acceptance.mjs` → **22/22 PASS**（证据：`artifacts/gap-ledger/ui_entry_acceptance.json`）。
覆盖：五工作区可达且无控制台错误 / Header 只读原因 / Blocker 原因与下一步 / Activity→Evidence（order·fill·execution_event 三类）
/ Orders 决策反查 / Monitor 可用动作 + L2 确认（`SUCCEEDED`）/ Assistant 输入·发送·追问·诊断路由·资本拒绝
/ Market（book/trades/heatmap/universe）/ System（limits/rate/latency/reconciliation/health）/ Performance（runs+metrics+钻取）
/ 不存在实体不崩页。

**守卫**：`test_ui_api_contract`、`test_surface_architecture`、`test_ui_module_hygiene` 全部通过（本轮两次误伤已按守卫本意修正源码，未放宽断言）。
**回归**：2560 passed / 0 failed / 24 skipped。
