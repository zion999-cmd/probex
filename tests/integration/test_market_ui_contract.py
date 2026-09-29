"""P0001.12 UI 契约（SC-1/7/11/12）：工作台页面只消费 Product API，不在浏览器重算。"""

from __future__ import annotations

import pathlib
import re
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
UI_ROOT = PROJECT_ROOT / "ui"
WORKBENCH_FILES = ("page.js", "heatmap.js", "timeline.js", "overlays.js", "replay.js")


class MarketWorkbenchUiContractTest(unittest.TestCase):
    def test_workbench_modules_exist(self) -> None:
        for name in WORKBENCH_FILES:
            with self.subTest(module=name):
                path = UI_ROOT / "pages" / "market" / name
                self.assertTrue(path.exists(), f"missing {name}")
                self.assertIn("export", path.read_text(encoding="utf-8"))

    def test_ui_only_talks_to_the_product_api(self) -> None:
        allowed = ("/api/v1/", "/ui/")
        for path in sorted((UI_ROOT / "pages" / "market").rglob("*.js")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertNotIn("binance", source.lower())
                self.assertNotIn("connectors", source)
                for url in re.findall(r'["\'](/[^"\']*)["\']', source):
                    self.assertTrue(any(url.startswith(prefix) for prefix in allowed),
                                    f"{path.name} references non-product URL {url}")

    def test_ui_never_recomputes_market_facts(self) -> None:
        """SC-7：特征值只能来自 projection，浏览器不得重算（无特征公式）。"""
        for path in sorted((UI_ROOT / "pages" / "market").rglob("*.js")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                for forbidden in ("ofi =", "imbalance =", "microprice =", "spread =",
                                  "best_bid +", "best_ask -"):
                    self.assertNotIn(forbidden, source)

    def test_unknown_is_rendered_with_a_reason(self) -> None:
        render = (UI_ROOT / "client" / "render.js").read_text(encoding="utf-8")
        self.assertIn("UNKNOWN", render)
        self.assertIn("reason", render)
        heatmap = (UI_ROOT / "pages" / "market" / "heatmap.js").read_text(encoding="utf-8")
        self.assertIn("UNKNOWN (no depth projection)", heatmap)

    def test_depth_heatmap_states_the_d025_invariant(self) -> None:
        heatmap = (UI_ROOT / "pages" / "market" / "heatmap.js").read_text(encoding="utf-8")
        self.assertIn("NOT fill evidence", heatmap)

    def test_replay_controls_only_post_to_replay_endpoints(self) -> None:
        replay = (UI_ROOT / "pages" / "market" / "replay.js").read_text(encoding="utf-8")
        self.assertIn("postReplay", replay)
        client = (UI_ROOT / "client" / "api.js").read_text(encoding="utf-8")
        self.assertIn("/api/v1/replay", client)
        for verb in ("play", "pause", "step"):
            self.assertIn(verb, replay)

    def test_workbench_endpoints_are_consumed(self) -> None:
        page = (UI_ROOT / "pages" / "market" / "page.js").read_text(encoding="utf-8")
        for endpoint in ("marketTimeline", "marketDepth", "marketTrades", "marketHealth", "marketOverlays"):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, page)
