# Probex UI 取证清单（只读截图）

- 取证时间：2026-10-25T10:42:14+08:00
- 产品入口：`http://127.0.0.1:8899/`（本机运行中的真实实例）
- 启动方式：`scripts/probex.sh start -d --port 8899`（PAPER，本地 event store）
- 浏览器：真实 Chrome headless；视口 **1728x960**（deviceScaleFactor=1），每页截整 viewport
- 动态观测：页面稳定后在正文内插入哨兵节点 + MutationObserver，观测 **10s**；同时统计主框架导航次数（判断是否有浏览器级整页 reload）
- 约束遵守：**未修改任何 CSS / JS / API / 运行逻辑**；未构造 synthetic 数据；UNKNOWN / NOT_AVAILABLE / 空态 / 降级态按产品真实状态保留
- 说明：PAPER 模式的本地行情来自产品自带的本地 event store（`~/.probex/local-run/events.jsonl`），UI 上明确标注 `LOCAL_TRIAL`（本地试验规则，非真实模型）；这不是为截图临时造的数据。
- 截图总数：**47** 张（32 个页面；其中 15 个页面因存在有意义的滚动区域补拍下半部分）

## 覆盖面

| 一级 workspace | 覆盖页面 |
|---|---|
| Monitor | `#/monitor` + overview / portfolio / orders 详情 + Assistant 展开 |
| Market | live / replay / run-review / universe / 公开行情视图(BNBUSDT) / 不支持交易对 + Assistant 展开 + run-review 详情 |
| Activity | 因果链 + orders / strategy / prediction / evidence 详情 + run 过滤 + Evidence 原始事实(order) |
| Performance | 绩效 + runs 分页 + metrics + run 详情 |
| System | health / risk / readiness / execution / instruments / connections / configuration / capabilities |

## 逐页清单

| # | route | 页面名称 | runtime mode | symbol | 页面状态 | 截图文件 | 是否发生整页刷新 |
|---|---|---|---|---|---|---|---|
| 1 | `/#/monitor` | Monitor（系统总览） | PAPER | BTCUSDT | 正常；含 UNKNOWN 与 NOT_AVAILABLE/UNAVAILABLE（产品真实状态，未伪造） | `screenshots/01-monitor.png`<br>`screenshots/01-monitor-lower.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=20 |
| 2 | `/#/monitor/overview` | Monitor → overview（详情） | PAPER | BTCUSDT | 正常 | `screenshots/02-monitor-overview.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 3 | `/#/monitor/portfolio` | Monitor → portfolio（详情） | PAPER | BTCUSDT | 正常；含 UNKNOWN（如实展示原因） | `screenshots/03-monitor-portfolio.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 4 | `/#/monitor/orders` | Monitor → orders（详情） | PAPER | BTCUSDT | 正常 | `screenshots/04-monitor-orders.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 5 | `/#/market/live` | Market → live（行情工作台） | PAPER | BTCUSDT | 正常；含 UNKNOWN 与 NOT_AVAILABLE/UNAVAILABLE（产品真实状态，未伪造） | `screenshots/05-market-live.png`<br>`screenshots/05-market-live-lower.png` | 否 — 局部更新（正文节点保留）；10s 内 mutations=12 |
| 6 | `/#/market/replay` | Market → replay（回放控件） | PAPER | BTCUSDT | 正常；含 UNKNOWN 与 NOT_AVAILABLE/UNAVAILABLE（产品真实状态，未伪造） | `screenshots/06-market-replay.png`<br>`screenshots/06-market-replay-lower.png` | 否 — 局部更新（正文节点保留）；10s 内 mutations=0 |
| 7 | `/#/market/run-review` | Market → run review（历史 run） | PAPER | BTCUSDT | 正常；含 UNKNOWN（如实展示原因） | `screenshots/07-market-run-review.png` | 否 — 局部更新（正文节点保留）；10s 内 mutations=0 |
| 8 | `/#/market/universe` | Market → universe（多标的平台） | PAPER | BTCUSDT | 正常 | `screenshots/08-market-universe.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=2 |
| 9 | `/#/market/live?view=BNBUSDT` | Market → 公开行情研究视图（BNBUSDT）（view symbol = BNBUSDT） | PAPER | BTCUSDT（runtime）／BNBUSDT（view） | 正常（公开行情研究视图，只读 REST）；部分能力 NOT_AVAILABLE/UNAVAILABLE 并附原因（交易所限流/无 L2 序列） | `screenshots/09-market-view-other-symbol.png` | 否 — 局部更新（正文节点保留）；10s 内 mutations=23 |
| 10 | `/#/market/live?view=NOPEZZZUSDT` | Market → 不支持交易对（错误反馈）（view symbol = NOPEZZZUSDT） | PAPER | BTCUSDT（runtime）／NOPEZZZUSDT（view） | 错误反馈（不支持交易对：接口 404 unsupported_symbol，页面给出原因与下一步，选择器仍可用） | `screenshots/10-market-unsupported-symbol.png` | 否 — 局部更新（正文节点保留）；10s 内 mutations=4 |
| 11 | `/#/activity` | Activity（因果链） | PAPER | BTCUSDT | 正常；含 UNKNOWN（如实展示原因） | `screenshots/11-activity.png`<br>`screenshots/11-activity-lower.png` | 否 — 局部更新（正文节点保留）；10s 内 mutations=0 |
| 12 | `/#/activity/orders` | Activity → orders（详情） | PAPER | BTCUSDT | 正常 | `screenshots/12-activity-orders.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 13 | `/#/activity/strategy` | Activity → strategy（详情） | PAPER | BTCUSDT | 正常；含 UNKNOWN（如实展示原因） | `screenshots/13-activity-strategy.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 14 | `/#/activity/prediction` | Activity → prediction（详情） | PAPER | BTCUSDT | 正常 | `screenshots/14-activity-prediction.png`<br>`screenshots/14-activity-prediction-lower.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 15 | `/#/activity/evidence` | Activity → evidence（详情） | PAPER | BTCUSDT | 正常；含 UNKNOWN（如实展示原因） | `screenshots/15-activity-evidence.png`<br>`screenshots/15-activity-evidence-lower.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=5 |
| 16 | `/#/performance` | Performance（绩效） | PAPER | BTCUSDT | 正常；含 UNKNOWN 与 NOT_AVAILABLE/UNAVAILABLE（产品真实状态，未伪造） | `screenshots/16-performance.png`<br>`screenshots/16-performance-lower.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=22 |
| 17 | `/#/performance/runs` | Performance → runs（分页列表） | PAPER | BTCUSDT | 正常 | `screenshots/17-performance-runs.png`<br>`screenshots/17-performance-runs-lower.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 18 | `/#/performance/metrics` | Performance → metrics（指标口径） | PAPER | BTCUSDT | 正常；含 UNKNOWN（如实展示原因） | `screenshots/18-performance-metrics.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 19 | `/#/system/health` | System → health | PAPER | BTCUSDT | 正常；含 UNKNOWN 与 NOT_AVAILABLE/UNAVAILABLE（产品真实状态，未伪造） | `screenshots/19-system-health.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 20 | `/#/system/risk` | System → risk | PAPER | BTCUSDT | 正常；含 UNKNOWN（如实展示原因） | `screenshots/20-system-risk.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 21 | `/#/system/readiness` | System → readiness | PAPER | BTCUSDT | 正常；含 UNKNOWN（如实展示原因） | `screenshots/21-system-readiness.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 22 | `/#/system/execution` | System → execution | PAPER | BTCUSDT | 内容偏少（空态） | `screenshots/22-system-execution.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=22 |
| 23 | `/#/system/instruments` | System → instruments | PAPER | BTCUSDT | 正常；含 UNKNOWN（如实展示原因） | `screenshots/23-system-instruments.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 24 | `/#/system/connections` | System → connections | PAPER | BTCUSDT | 正常；含 UNKNOWN（如实展示原因） | `screenshots/24-system-connections.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 25 | `/#/system/configuration` | System → configuration | PAPER | BTCUSDT | 正常 | `screenshots/25-system-configuration.png`<br>`screenshots/25-system-configuration-lower.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 26 | `/#/system/capabilities` | System → capabilities | PAPER | BTCUSDT | 正常 | `screenshots/26-system-capabilities.png`<br>`screenshots/26-system-capabilities-lower.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |
| 27 | `/#/monitor` | Monitor + Assistant Drawer（展开） | PAPER | BTCUSDT | 正常；含 UNKNOWN 与 NOT_AVAILABLE/UNAVAILABLE（产品真实状态，未伪造） | `screenshots/27-monitor-assistant.png`<br>`screenshots/27-monitor-assistant-lower.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=20 |
| 28 | `/#/market/live` | Market + Assistant Drawer（展开） | PAPER | BTCUSDT | 正常；含 UNKNOWN 与 NOT_AVAILABLE/UNAVAILABLE（产品真实状态，未伪造） | `screenshots/28-market-assistant.png`<br>`screenshots/28-market-assistant-lower.png` | 否 — 局部更新（正文节点保留）；10s 内 mutations=12 |
| 29 | `/#/performance/run/paper-1792895224921` | Performance → run 详情（paper-1792895224921） | PAPER | BTCUSDT | 正常；含 UNKNOWN 与 NOT_AVAILABLE/UNAVAILABLE（产品真实状态，未伪造） | `screenshots/29-performance-run-detail.png`<br>`screenshots/29-performance-run-detail-lower.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=22 |
| 30 | `/#/market/run-review/paper-1792895224921` | Market → run review 详情（paper-1792895224921） | PAPER | BTCUSDT | 正常；含 UNKNOWN（如实展示原因） | `screenshots/30-market-run-review-detail.png` | 否 — 局部更新（正文节点保留）；10s 内 mutations=0 |
| 31 | `/#/activity/run/paper-1792895224921` | Activity → run 过滤（paper-1792895224921） | PAPER | BTCUSDT | 正常；含 UNKNOWN（如实展示原因） | `screenshots/31-activity-run-detail.png`<br>`screenshots/31-activity-run-detail-lower.png` | 否 — 局部更新（正文节点保留）；10s 内 mutations=0 |
| 32 | `/#/activity/evidence/order/probex-paper-1792895224921-000109` | Activity → Evidence 原始事实（order probex-paper-1792895224921-000109） | PAPER | BTCUSDT | 正常；含 UNKNOWN（如实展示原因） | `screenshots/32-activity-evidence-order.png`<br>`screenshots/32-activity-evidence-order-lower.png` | 否 — 整面重渲染（正文 innerHTML 被替换，无浏览器导航）；10s 内 mutations=4 |

## 动态更新行为汇总（10s 观测）

| 观测到的更新形式 | 页面数 | 说明 |
|---|---|---|
| 整面重渲染（正文 innerHTML 被替换，无浏览器导航） | 23 | console 轮询后整体重渲染该 surface（哨兵节点被移除），但**没有**浏览器导航/白屏 |
| 局部更新（正文节点保留） | 9 | 页面实现了非破坏性 refresh（图表/画线/选中状态保留），只有数据区域更新 |

关键结论：**全部 32 个页面在 10s 观测窗口内均未发生浏览器级整页 reload**；Market / Activity / run-review 等页为局部更新，其余为 surface 级重渲染。

## 未覆盖/受限说明

- Testnet / LIVE 模式：未启用（需独立授权），本次取证只有 PAPER/REPLAY 可达的页面。
- 真实公网行情组件：受本机网络与交易所限流影响，部分组件呈 UNAVAILABLE（已在页面与清单中如实记录，未伪造）。
- 截图只能证明“渲染出什么”，不证明后端数据正确性；数据正确性由既有测试与验收报告另行覆盖。
