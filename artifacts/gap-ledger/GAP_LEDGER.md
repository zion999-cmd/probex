# Probex 全局需求—实现—验收缺口台账

**建立时间**：2026-10-10（全局产品补齐任务 §二产物）
**依据**：`CLAUDE.md` + 全部 41 个 Proposal + `context/*` + ADR/Contract/Schema
**状态图例**：

- ✅ 已实现且有端到端验收证据
- 🟡 已实现但证据不充分 / 证据陈旧
- 🔴 部分实现 / 实际不可用
- ⛔ 尚未实现
- 📦 明确延期 / 被原 Proposal 排除
- ❓ 因缺证据无法确认

> 本台账是全局任务的**持续维护**文件；每批实施完成后更新对应行。

---

## 1. 逐 Proposal 总账

| # | Proposal | 文件状态 | 实现事实 | 判定 | 证据 |
|---|---|---|---|---|---|
| 1 | P0001.1 Market Event/L2 Book/BookHealth | 已完成 | 边界/盘口/健康机全在 | ✅ | 单测+fault |
| 2 | P0001.2 Event Store/Replay | 已完成 | 追加/序号/内容寻址/重放全在 | ✅ | replay 套件 |
| 3 | P0001.3 MarketState/Feature Engine | 已完成 | 六段 feature/窗口/质量闸 | ✅（trade 段后由 D-054 补齐） | 单测 |
| 4 | P0001.4 Jev Prediction Runtime | 已完成 | schema/scheduler/runtime 全在 | ✅ | 全 FakeClock |
| 5 | P0001.4.1 Real Jev Transport | 已完成 | 实验结论 VALIDATED/REJECTED | ✅ | 真实探测 |
| 6 | P0001.4.2 Native Typed Jev | 已完成 | System One typed 热路径 | ✅ | 3 次真实调用 |
| 7 | P0001.5 Accounting/Risk Core | **已批准（实施中）** ⚠️ | 全部能力 + commit `1c39e26` 已落地 | ✅（**状态字段陈旧，需纠正**） | 单测/replay |
| 8 | P0001.6 Order Lifecycle/Paper | **已批准（实施中）** ⚠️ | 全部能力 + `7053a70` | ✅（状态字段陈旧） | 集成/fault |
| 9 | P0001.6.1 Uncertain Exposure | **已批准（实施中）** ⚠️ | 全部能力 + `d510684` | ✅（状态字段陈旧） | fault |
| 10 | P0001.7 Maker Policy | 已完成 | 报价/规模/库存/生命周期 | ✅ | 集成 |
| 11 | P0001.7.1 Prediction Outage Continuity | **已批准（实现中）** ⚠️ | 全部能力 + `6e8d035` | ✅（状态字段陈旧） | fault |
| 12 | P0001.8 Event-level Fill Sim | 已完成 | SimulatedVenue 全 19 SC | ✅ | 单测 |
| 13 | P0001.9.1 Binance Public Data | 已完成 | 真实公网 connector + smoke | ✅（历史真实证据） | 真实公网验收 |
| 14 | P0001.9.1.1 Depth Continuity | 已完成 | pu 判据修正 + 真实流 | ✅ | 真实流核验 |
| 15 | P0001.9.2 Private User Stream | 已完成 | 测试网真实验证 | ✅（主网未验证） | 真实事件链 |
| 16 | P0001.9.2.1 Private Contract Audit | 已完成 | 裁决 KEEP_NATIVE_PRIVATE | ✅ | 审计+PoC |
| 17 | P0001.9.3 Startup Recovery | 已完成 | RecoveryGate + baseline | ✅（测试网真实 PASS） | 真实只读 |
| 18 | P0001.9.3.1 Recovery Closure | 已完成 | ownership/三分类/失效接线 | ✅ | 测试网真实 |
| 19 | P0001.9.3.2 Discontinuity Reliability | 已完成 | observer 隔离+审计 | ✅ | 单测 |
| 20 | P0001.9.4 Live Readiness Gate | 已完成 | gate/evidence/clock | ✅（测试网 blocked→后 HWM 解除） | 真实只读 |
| 21 | P0001.9.4.1 Historical Risk Bootstrap | 已完成 | income 历史 + daily PnL | ✅ | 真实 income |
| 22 | P0001.9.4.1.1 Full Readiness Integration | 已完成 | 全链真实验证 | ✅ | 真实全链 |
| 23 | P0001.9.4.2 Persistent Equity HWM | 已完成 | durable HWM + live_ready | ✅ | 跨进程真实 |
| 24 | P0001.9.5 Readiness Evidence Binding | 已完成 | typed evidence/authority/generation | ✅ | 测试网真实 |
| 25 | P0001.9.6 Binance Execution Adapter | 已完成 | 真实写链 + UNKNOWN 不重试 | ✅（测试网真实） | 真实写验收 |
| 26 | P0001.9.7 Live Orchestration | 已完成（dev scope） | Phase A observe-only PASS | 📦 Phase B ≥30min **deferred** | 真实 10 min |
| 27 | P0001.9.7.1 Private Latency Bootstrap | 已完成 | 机制全；verdict C | 📦 live happy path deferred | 机制单测 |
| 28 | P0001.9.7.2 Normalization Ownership | 已完成 | Decimal 归一 ownership | ✅ | 单测+真实 smoke |
| 29 | P0001.10 Product API/UI | 已完成 | 读模型/只读 API/控制台 | ✅ | 集成 |
| 30 | P0001.10.2 UI Reports | 已完成 | reports + 10 页控制台 | ✅ | 集成 |
| 31 | P0001.10.3 Product CLI | 已完成 | 只读 CLI/退出码 | ✅ | 单测 |
| 32 | P0001.11 Product Ops Foundation | 已完成 | provenance/registry/metrics/blockers/capabilities | ✅ | 单测 |
| 33 | P0001.11.1 Run Lifecycle | 已完成 | RuntimeSession | ✅ | 单测 |
| 34 | P0001.11.2 Session Wiring | 已完成 | SessionHost 四模式 | ✅ | 真实四模式 |
| 35 | P0001.12 Visual Workbench | 已完成 | timeline/热图/replay 控件 | ✅ | 集成 |
| 36 | P0001.12.1 Surface Architecture | 已完成 | 5 Surface + gap audit | ✅ | 单测 |
| 37 | P0001.12.2 Surface Capabilities | 已完成 | G1–G5 事实端点 | ✅ | 集成 |
| 38 | P0001.12.3 Assistant/Action Gateway | 已完成 | Manifest/Gateway/Assistant | ✅ | 集成 |
| 39 | P0001.13 Execution Safety Ops | 已完成 | policy/governor/latency/health | ✅ | 单测+真实 usage |
| 40 | P0001.14 Runtime Decision Loop | 已完成 | Market→Risk→PAPER 接线 | ✅ | 集成 E2E |
| 41 | P0001.15 Instrument/Venue Contract | 已完成 | instrument 域 + venue 契约 | ✅ | SC-24…28 |
| 42 | P0001.16 Venue Execution Productization | **实现中 / Deferred** | 隔离 acceptance + 真实成交已发生 | 🔴 SC-11/12/13/6/7/15 未达成 + 残留 0.001 BTC | 部分真实 |
| 43 | P0001.17 Local Trading Loop | 已完成 | 本地闭环 + LOCAL_TRIAL + Run Review + 服务端指标 | ✅ | E2E+浏览器 |

---

## 2. 缺口清单（按任务 §三 分类）

### A. 实时行情与标的覆盖

| # | 事实 | 判定 |
|---|---|---|
| A-1 | **产品入口只支持单标的 BTCUSDT 永续**：`domain/instruments/registry.py::PRODUCTION_ASSET_CLASSES={CRYPTO}`、`PRODUCTION_PRODUCT_TYPES={PERPETUAL}`、单 registry 实例 | 📦 **原始 Proposal 就是单标的生产范围**（多标的/股票/期货仅 vocabulary，属 NOT Included）；非缺陷 |
| A-2 | ✅ 已修正：产品入口支持 `--market-source binance-public`（既有真实公网 connector；只读无需凭据）|
| A-3 | 断线/重连/缺口/健康在真实 connector 层均有实现（P0001.9.1/.1.1） | ✅（连接器层）；🟡（未在产品入口暴露） |
| A-4 | ~~`--mode testnet` + event store 时 UI 误显 `binance:market CONNECTED`~~ ✅ 已修正：产品入口在 TESTNET/LIVE 模式无专用栈时直接拒绝启动 |

### B. 持续采集与本地持久化

| # | 事实 | 判定 |
|---|---|---|
| B-1 | Run 级事实已持久化（`<id>.market/facts/trades.jsonl`），重启可读 | ✅（D-054 + 逐笔补齐；实测历史 run 181/181 带量） |
| B-2 | ✅ 已修正：binance-public 源在 pump 线程上持续采集并把 market/trades 事实写入既有 run registry |
| B-3 | 无第二套存储（run registry 是唯一 Owner） | ✅ 边界完好 |

### C. 图表与历史浏览

| # | 事实 | 判定 |
|---|---|---|
| C-1 | K 线=服务端聚合（含 vwap）+ 服务端 ATR；真实浏览器 181 candles/markers 验证 | ✅ |
| C-2 | 历史 run 行情可查（source=durable），图表可定位 | ✅ |
| C-3 | 标的切换 UI：无（单标的） | 📦 与 A-1 同因 |
| C-4 | 无空壳/失效入口（本次 grep 无 coming-soon/TODO 控件） | ✅ |

### D. 交易闭环与运行状态

| # | 事实 | 判定 |
|---|---|---|
| D-1 | PAPER 本地闭环全链有 E2E（decision→risk→fill→ledger→PnL→API/UI） | ✅ |
| D-2 | TESTNET 真实闭环：acceptance 已能真实成交，但 SC-12（完整端到端+清场）未达成 | 🔴 **G-D2**（=P0001.16，阻塞于交易所写入授权） |
| D-3 | UNKNOWN≠0/EMPTY/健康；UNKNOWN 不重试 | ✅（守卫+fault 测试） |
| D-4 | PAPER/REPLAY/TESTNET/LIVE 四模式语义存在 | ✅（但 TESTNET/LIVE 在产品入口是"标签+无连接器"，见 G-A4） |

### E. 产品入口与操作

| # | 事实 | 判定 |
|---|---|---|
| E-1 | CAPITAL 全部 unavailable_by_design；Gateway 拒绝注册 CAPITAL handler | ✅ |
| E-2 | HTTP 只读（非 GET 405）；runtime/stop=501；replay 控制仅本地 | ✅ |
| E-3 | 启动脚本 4 个（用户反馈过多） | 🔴 **G-E3**：合并为 1 个 |
| E-4 | 无"看似可用的空入口" | ✅ |

### F. 治理文档一致性

| # | 缺口 | 判定 |
|---|---|---|
| G-F1 | 4 个 Proposal 状态字段陈旧（P0001.5/.6/.6.1/.7.1） | 🔴 纠正为 已完成 |
| G-F2 | roadmap 缺 P0001.16/.17 + 9.7 状态矛盾 | 🔴 补正 |
| G-F3 | current_state 版本表缺 16/17 + P0001.17 段陈旧 + 孤立残句 | 🔴 补正 |
| G-F4 | D-054 第 2 条被逐笔提交取代；indicators.js 顶部陈旧重复 docstring | 🔴 补正 |
| G-F5 | `artifacts/p0001.17/local_loop_capture.json` 陈旧（含失败项） | 🟡 重跑归档或标注 |

### G. 模型与策略效果（任务 §八 要求如实回答）

| # | 事实 | 判定 |
|---|---|---|
| G-G1 | **无任何预测优势/策略有效性/风险调整收益/成本模型真值证据** | ❓ 未知；`LOCAL_TRIAL` 非模型。属"实验/标定"范畴，原 Proposal 未授权实施 |

---

## 3. 实施批次计划（按依赖排序）

| 批次 | 内容（缺口） | 可否在"无交易所写入"下完成 |
|---|---|---|
| **Batch 1：事实一致性收敛** ✅ 已完成（2454 passed） | G-F1/F2/F3/F4/F5、G-E3（合并脚本） | ✅ 全部可 |
| **Batch 2：诚实性修正** ✅ 已完成（2459 passed） | G-A4（模式标签 vs 事实） | ✅ 可（只改产品投影/启动校验，不下单） |
| **Batch 3：真实行情接入** ✅ 已完成（2461 passed；真实公网验收因 Binance 451 geo-block NOT RUN，改离线脚本化集成验证） | G-A2/G-B2（既有公网 connector 经产品入口接线 ⇒ 真实持续采集/持久化） | ✅ 只读可（公网无需凭据） |
| **Batch 4：P0001.16 收口** | G-D2（SC-11/12/13/6/7/15 + 清场 0.001 BTC） | ⛔ **需要人类单独授权 TESTNET 写入/平仓**（任务 §六）；未授权前保持 Blocked |
| **Batch 5（待决，不自行实施）** | G-G1 策略/模型有效性标定；A-1 多标的（若人类改范围） | ❓ 需新提案/明确业务授权 |

---

## 4. 台账维护记录

- 2026-10-10 建立（基于同日只读全项目审计；41 Proposal 全覆盖）。
- 2026-10-10 Batch 1 完成：4 个陈旧 Proposal 状态纠正；roadmap/current_state/decisions 补正；启动脚本合并为单个 `scripts/probex.sh`（up/demo/status/data）；陈旧 capture 证据重跑归档（missing=NONE）。
- 2026-10-10 Batch 2 完成：`runtime.assembly` 在 TESTNET/LIVE 模式无专用栈时 fail-fast 拒绝（消除‘本地文件冒充 binance:market CONNECTED’的标注缺陷）。
- 2026-10-10 Batch 3 完成：`--market-source binance-public` 把产品入口接到既有真实公网 connector（pump 线程 + 既有 run registry 持久化）；新增 `runtime/public_market.py` 与离线集成测试；真实公网验收因当前网络被 Binance 451 geo-block（NOT RUN，诚实标注）。
- 2026-10-10 修正：`docs/RUNBOOK.md` 被全局 gitignore 的 `docs` 规则静默排除（clean checkout 缺文件）⇒ force-track 该交付物；detached checkout 复核 2461 全绿。
