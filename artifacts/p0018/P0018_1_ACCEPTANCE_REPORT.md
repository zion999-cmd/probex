# P0018_1 Acceptance Report — UI 工作台信息架构与稳定渲染 Phase 1

日期：2026-10-25　状态：**PASS**（实现侧；浏览器证据见 `p0018_result.json` + 4 张截图）
范围：Global Shell blocker 聚合、Monitor IA 重构 + targeted rendering、Assistant 默认收起；不触碰 Market/Activity/Performance/System 与任何交易/风险/会计逻辑。

## SC 矩阵

| SC | 内容 | 结果 | 证据 |
|---|---|---|---|
| 1 | 10s 内 0 reload / 0 whole-workspace replacement | PASS | CDP MutationObserver：reloads=0，viewChildrenReplaced=0 |
| 2 | workspace root / Assistant / 静态容器 identity 保持 | PASS | sameView/sameAssistant/staticStable=true |
| 3 | 刷新不重置滚动 | PASS | #view scrollTop 250→250（注入 overflow 探针制造真实滚动） |
| 4 | Assistant 展开 + 未发送输入保留 | PASS | drawerOpen=true，draft 保留「为什么现在没有下单？(draft)」 |
| 5 | 顶部无 raw blocker/event rows | PASS | `.blocker-item`=0；条带=「● BLOCKING · 2 blockers · RISK 1 · VENUE 1」 |
| 6 | DEGRADED/BLOCKING 第一屏可发现且可导航证据 | PASS | health-pill=BLOCKING；href=`#/activity/evidence?focus=blockers`；Evidence 页 focus 锚定 `#evidence-blockers` |
| 7 | 第一屏无需滚动判断 mode/runtime/health/equity/PnL/position/exposure/三个 health/blockers | PASS | 14 个 data-bind 齐备；primary band top/bottom 均在 1728×960 首屏 |
| 8 | UNKNOWN/NOT_AVAILABLE 不被伪装成 healthy | PASS | unknown 节点带 class+title(reason)；无空占位；Readiness=unavailable 橙色显式 |
| 9 | 交易/风险/记账/execution 行为不变 | PASS | 仅 UI/CSS 改动；全量 2698 passed |
| 10 | 既有测试通过 + 新增稳定 DOM/刷新测试 | PASS | 新增 `tests/unit/test_p0018_ui_shell.py`（9）；CDP `tests/ui/p0018_shell_stability.mjs`（15 断言） |
| 11 | 1728×960 重拍 4 张 | PASS | collapsed / assistant-expanded / scrolled / blocker-drilldown（本目录 PNG） |
| 12 | 收口后停止，不自行重构其他 workspace | PASS | 未改 Market/Activity/Performance/System 页面结构 |

## 关键实现

- `ui/app/index.html`：`#strip` 内 `#blockers` raw 区 → 单一 `#global-status` 锚点（默认 collapsed 不占主区）。
- `ui/client/console.js`：`summarizeBlockers()` 纯函数聚合（OK/DEGRADED/BLOCKING + 按 owner 计数），strip 只 patch textContent/dataset/title。
- `ui/assistant/drawer.js`：默认 collapsed；展开态写 sessionStorage（`probex.assistant.open`），surface/数据刷新不改变。
- `ui/pages/monitor/page.js`：重构为 primary（statusline + 5 KPI + 4 health chips + attention）/ trends / 2 个短列表 secondary / 折叠 diagnostic；
  `render()` 首帧即填真实值（Node render harness 兼容），`refresh()` 只 patch `data-bind` 节点与列表容器；动作降噪为 Inspect issues / View activity / Reconcile（仅 AVAILABLE）。
- `ui/monitor/sparklines.js`：图表实例按 host 复用（setOption），host 节点 identity 稳定。
- `ui/pages/evidence/page.js`：新增 `#evidence-blockers` + mount 时 `focus=blockers` 锚定（不重构 Activity）。
- 旧契约测试按 P0018 新语义更新：`test_surface_architecture`（Monitor 必需字段/条带）、`render_page.mjs`（新增 console.js/actions.js 依赖）；新增静态守卫常驻 unittest。

## 证据文件

- `p0018_result.json`：15/15 CDP 断言明细（base=http://127.0.0.1:8892，本地 PAPER 闭环）。
- 4 张截图（1728×960）。

## 复测方式

```bash
python3 tests/ui/local_paper_demo.py 8892 /tmp/p0018demo/demo.json 1 &
node tests/ui/p0018_shell_stability.mjs http://127.0.0.1:8892 artifacts/p0018
python3 -m unittest tests.unit.test_p0018_ui_shell   # 随 discover 常驻
```

## 未包含（Phase 1 边界）

Market/Activity/Performance/System 重构；因果链可视化；Performance 研究比较闭环；均留待后续 Proposal。
