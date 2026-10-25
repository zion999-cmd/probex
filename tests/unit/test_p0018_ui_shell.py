"""P0018 静态契约守卫（随 unittest 常驻运行）：

- 全局 blocker 条带已聚合（无原始事件行）；
- Assistant 默认收起且展开态持久；
- Monitor 新 IA：Primary 必须含 mode/runtime/health/equity/PnL/position/exposure/
  market/strategy/execution/blockers，且提供 targeted `refresh()`；
- Monitor 不再平铺全量 Action Manifest（只留上下文相关的少量操作）；
- Evidence 支持 focus=blockers 锚定。

真实 DOM identity/scroll/输入保留由 CDP 验收脚本 tests/ui/p0018_shell_stability.mjs
驱动实际产品验证（与 ui_entry_acceptance.mjs 同约定）。
"""

from __future__ import annotations

import pathlib
import re
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
UI = PROJECT_ROOT / "ui"


def read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


class GlobalShellContractTest(unittest.TestCase):
    def test_raw_blocker_log_strip_replaced_by_aggregated_summary(self) -> None:
        index = read("app/index.html")
        self.assertIn('id="global-status"', index)
        self.assertNotIn('id="blockers"', index)

        console = read("client/console.js")
        self.assertIn("export function summarizeBlockers", console)
        # 原始 blocker 事件行不再由全局条带渲染
        self.assertNotIn("blocker-item", console)
        self.assertNotIn("strip.innerHTML", console)
        self.assertIn("strip.textContent = summary.text", console)
        # 聚合语义：状态分级 + 按 owner 计数
        for state in ("BLOCKING", "DEGRADED", "OK"):
            self.assertIn(state, console)

    def test_summary_groups_blockers_by_owner(self) -> None:
        # 轻量行为镜像：分组规则的纯字符串契约（CDP 脚本做真实 DOM 验证）
        console = read("client/console.js")
        self.assertIn("groups.set(owner, (groups.get(owner) || 0) + 1)", console)
        self.assertIn("severity === \"BLOCKING\"", console)


class AssistantDrawerContractTest(unittest.TestCase):
    def test_drawer_defaults_collapsed_and_persists_open_state(self) -> None:
        drawer = read("assistant/drawer.js")
        self.assertIn("DRAWER_STATE_KEY", drawer)
        self.assertIn("classList.toggle(\"collapsed\", !drawerShouldBeOpen())", drawer)
        self.assertIn("sessionStorage.setItem(DRAWER_STATE_KEY", drawer)


class MonitorInformationArchitectureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.page = read("pages/monitor/page.js")

    def test_primary_band_carries_all_required_binds(self) -> None:
        # 绑定键要么直接出现在 data-bind="x"，要么经 kpi()/healthChip() 构造
        for bind in ("mode", "instrument", "runtime_state", "overall_health",
                      "equity", "realized_pnl", "unrealized_pnl", "position", "exposure",
                      "market_health", "strategy_state", "execution_health", "readiness",
                      "attention"):
            pattern = rf'data-bind="{bind}"|(?:kpi|healthChip)\([^\n]*"{bind}"'
            self.assertRegex(self.page, pattern, f"primary band missing bind {bind}")

    def test_page_exports_targeted_refresh(self) -> None:
        self.assertRegex(self.page, r"export\s+async\s+function\s+refresh\b")
        # refresh 只 patch 节点，不重建 workspace（不得把整个 view 重塞 innerHTML）
        self.assertNotIn("getElementById(\"view\")", self.page)

    def test_actions_reduced_to_contextual_set(self) -> None:
        # 不再把 Manifest 全量动作渲染成按钮
        self.assertNotIn("actionsSection", self.page)
        self.assertNotIn("runnable now", self.page)
        # 仅保留：Inspect issues / View activity 链接 + reconcile（且必须 AVAILABLE）
        self.assertIn("Inspect issues", self.page)
        self.assertIn("View activity", self.page)
        self.assertIn('"runtime.request_reconciliation"', self.page)
        self.assertIn('state === "AVAILABLE"', self.page)

    def test_raw_blocker_rows_not_rendered_diagnostics_collapsed(self) -> None:
        self.assertNotIn("Blockers / warnings", self.page)
        self.assertIn('<details class="diagnostics', self.page)
        # 完整工程视图通过链接到达（不在 Monitor 平铺）
        self.assertIn('href="#/system"', self.page)
        self.assertIn('href="#/activity/evidence', self.page)

    def test_unknown_semantics_preserved_in_patch(self) -> None:
        # UNKNOWN 仍显式（unknown 类 + reason 进 title），不伪装成 healthy
        self.assertIn('cls: factValue.known ? "known" : "unknown"', self.page)
        self.assertIn("factValue.reason", self.page)


class EvidenceFocusContractTest(unittest.TestCase):
    def test_blockers_section_is_focus_target(self) -> None:
        evidence = read("pages/evidence/page.js")
        self.assertIn('id="evidence-blockers"', evidence)
        self.assertIn("focus", evidence)
        self.assertRegex(evidence, r"export\s+async\s+function\s+mount\b")


if __name__ == "__main__":
    unittest.main()
