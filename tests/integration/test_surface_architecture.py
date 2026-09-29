"""P0001.12.1 Product Surface Architecture（SC-1 – SC-12，全部结构性验证）。"""

from __future__ import annotations

import pathlib
import re
import unittest

from api.routes import (
    CAPABILITIES_PATH,
    FACTS_PATH,
    MARKET_DEPTH_PATH,
    MARKET_HEALTH_PATH,
    MARKET_OVERLAYS_PATH,
    MARKET_TIMELINE_PATH,
    MARKET_TRADES_PATH,
    METRICS_PATH,
    PORTFOLIO_TIMELINE_PATH,
    REPLAY_PATH,
    REPORT_PATH,
    ROUTES,
    RUNS_COMPARE_PATH,
    RUNS_PATH,
)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
UI_ROOT = PROJECT_ROOT / "ui"
SURFACE_SLUGS = ("monitor", "market", "activity", "performance", "system")


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class SurfaceArchitectureTest(unittest.TestCase):
    def test_1_top_level_navigation_is_exactly_five_surfaces(self) -> None:
        surfaces = read(UI_ROOT / "app" / "surfaces.js")
        entries = re.findall(r'slug: "([a-z-]+)"', surfaces)

        self.assertEqual(entries, list(SURFACE_SLUGS))
        self.assertLessEqual(len(entries), 5)

    def test_2_monitor_answers_current_system_state(self) -> None:
        monitor = read(UI_ROOT / "pages" / "monitor" / "page.js")
        for required in ("equity", "realized pnl", "unrealized pnl", "position", "open order exposure",
                         "uncertain exposure", "active orders", "readiness", "Blockers", "Recent activity"):
            with self.subTest(field=required):
                self.assertIn(required, monitor)

    def test_3_market_answers_what_the_market_is_doing(self) -> None:
        market = read(UI_ROOT / "pages" / "market" / "page.js")
        for required in ("heatmap", "Features", "Data quality", "Trades", "Decisions", "Execution",
                         "Replay controls"):
            with self.subTest(field=required):
                self.assertIn(required, market)
        for view in ("live", "replay", "run-review"):
            self.assertIn(view, market)

    def test_4_activity_answers_why(self) -> None:
        activity = read(UI_ROOT / "pages" / "activity" / "page.js")
        for stage in ("market_state", "prediction", "maker_decision", "readiness", "order",
                      "execution_event"):
            with self.subTest(stage=stage):
                self.assertIn(stage, activity)

    def test_5_performance_is_run_centric(self) -> None:
        performance = read(UI_ROOT / "pages" / "performance" / "page.js")
        self.assertIn("Runs (run is the unit)", performance)
        self.assertIn("runCompare", performance)
        self.assertIn("Metric contract", performance)

    def test_6_system_answers_why_it_can_or_cannot_run(self) -> None:
        system = read(UI_ROOT / "pages" / "system" / "page.js")
        for name in ("health", "risk", "readiness", "execution", "configuration", "capabilities"):
            with self.subTest(section=name):
                self.assertIn(f'"{name}"', system)

    def test_7_prediction_strategy_and_evidence_are_not_top_level(self) -> None:
        surfaces = read(UI_ROOT / "app" / "surfaces.js")
        for legacy in ("prediction", "strategy", "evidence"):
            with self.subTest(page=legacy):
                self.assertNotIn(f'slug: "{legacy}"', surfaces)
                self.assertIn(legacy, surfaces)   # 作为 detail view 归属到某个 Surface

    def test_8_9_execution_safety_has_a_system_slot(self) -> None:
        system = read(UI_ROOT / "pages" / "system" / "page.js")
        for field in ("rate limits", "venue limits", "latency", "reconciliation", "unknown exposure flag"):
            with self.subTest(field=field):
                self.assertIn(field, system)

    def test_10_no_orphan_pages(self) -> None:
        """每个既有页面都必须有明确归属（Surface 或 legacy→Surface 映射）。"""
        surfaces = read(UI_ROOT / "app" / "surfaces.js")
        mapping = re.findall(r'^\s*([a-z-]+): "([a-z-]+)",', surfaces, flags=re.MULTILINE)
        home = dict(mapping)
        page_dirs = {path.parent.name for path in (UI_ROOT / "pages").glob("*/page.js")}
        for page in sorted(page_dirs):
            with self.subTest(page=page):
                self.assertTrue(page in SURFACE_SLUGS or page in home,
                                f"{page} has no Surface home (orphan page)")
        for legacy, surface in home.items():
            with self.subTest(legacy=legacy):
                self.assertIn(surface, SURFACE_SLUGS)

    def test_11_ui_uses_only_existing_product_endpoints(self) -> None:
        """SC-11：UI 不得引入第二套事实来源（只能用已注册的 Product API 路径）。"""
        # UI 只能引用**已注册**的 Product API 表面（含 server 侧的专用端点）
        known = {"/api/v1/snapshot", "/api/v1/schema", REPORT_PATH, CAPABILITIES_PATH, METRICS_PATH,
                 RUNS_PATH, RUNS_COMPARE_PATH, REPLAY_PATH, MARKET_TIMELINE_PATH, MARKET_DEPTH_PATH,
                 MARKET_TRADES_PATH, MARKET_HEALTH_PATH, MARKET_OVERLAYS_PATH, FACTS_PATH,
                 PORTFOLIO_TIMELINE_PATH}
        known |= set(ROUTES)
        known |= {"/api/v1/execution"}   # execution 路由别名（orders 的兼容路径）
        for path in sorted(UI_ROOT.rglob("*.js")):
            source = read(path)
            for url in re.findall(r'"(/api/v1/[^"?]*)', source):
                with self.subTest(module=path.name, url=url):
                    self.assertTrue(url in known or url.rstrip("/") in known
                                    or any(url.startswith(prefix) for prefix in ("/api/v1/runs/",)),
                                    f"{path.name} references unknown endpoint {url}")

    def test_12_ui_adds_no_trading_write_path(self) -> None:
        client = read(UI_ROOT / "client" / "api.js")
        posts = re.findall(r'method: "POST"', client)
        self.assertEqual(len(posts), 1)                       # 唯一 POST 是 replay control
        self.assertIn("postReplay", client)
        for path in sorted(UI_ROOT.rglob("*.js")):
            source = read(path).lower()
            with self.subTest(module=path.name):
                for forbidden in ("/api/v1/buy", "/api/v1/sell", '/api/v1/order"', "/api/v1/position",
                                  "set-leverage", "set-risk"):
                    self.assertNotIn(forbidden, source)

    def test_header_and_blocker_strip_are_global(self) -> None:
        shell = read(UI_ROOT / "app" / "index.html")
        console = read(UI_ROOT / "client" / "console.js")
        self.assertIn('id="banner"', shell)
        self.assertIn('id="blockers"', shell)
        for field in ("mode", "environment", "venue", "symbol", "runtime_id", "health", "data_timestamp"):
            with self.subTest(field=field):
                self.assertIn(field, console)

    def test_mode_and_unknown_rendering_rules_are_present(self) -> None:
        console = read(UI_ROOT / "client" / "console.js")
        self.assertIn("id.mode", console)                     # Mode 永远显示
        render = read(UI_ROOT / "client" / "render.js")
        self.assertIn("UNKNOWN", render)
        self.assertIn("reason", render)                       # UNKNOWN 必须带原因
