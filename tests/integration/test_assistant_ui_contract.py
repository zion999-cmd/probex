"""P0001.12.3 UI 契约：Assistant Drawer 只能使用 Manifest 声明的动作（SC-11/12）。"""

from __future__ import annotations

import pathlib
import re
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
UI_ROOT = PROJECT_ROOT / "ui"
ASSISTANT_FILES = ("drawer.js", "context.js", "actions.js")


class AssistantUiContractTest(unittest.TestCase):
    def test_assistant_modules_exist(self) -> None:
        for name in ASSISTANT_FILES:
            with self.subTest(module=name):
                path = UI_ROOT / "assistant" / name
                self.assertTrue(path.exists(), f"missing {name}")
                self.assertIn("export", path.read_text(encoding="utf-8"))

    def test_drawer_is_mounted_globally_not_as_a_sixth_surface(self) -> None:
        console = (UI_ROOT / "client" / "console.js").read_text(encoding="utf-8")
        surfaces = (UI_ROOT / "app" / "surfaces.js").read_text(encoding="utf-8")

        self.assertIn("mountAssistant", console)
        self.assertNotIn("assistant", re.findall(r'slug: "([a-z-]+)"', surfaces))

    def test_actions_come_from_the_manifest_only(self) -> None:
        actions = (UI_ROOT / "assistant" / "actions.js").read_text(encoding="utf-8")

        self.assertIn("assistantContext", actions)   # suggestions 来自 manifest 端点
        self.assertIn("renderActions", actions)
        for forbidden in ("capital.", "place_order", "set_leverage", "set_risk_limits", "buy", "sell"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, actions)

    def test_ui_only_talks_to_the_product_api(self) -> None:
        for name in ASSISTANT_FILES:
            source = (UI_ROOT / "assistant" / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertNotIn("binance", source.lower())
                for url in re.findall(r'"(/api/v1/[^"?]*)', source):
                    self.assertTrue(url.startswith("/api/v1/"), url)

    def test_confirmation_flow_is_bound_to_the_returned_id(self) -> None:
        drawer = (UI_ROOT / "assistant" / "drawer.js").read_text(encoding="utf-8")

        self.assertIn("CONFIRMATION_REQUIRED", drawer)
        self.assertIn("confirmation_id", drawer)

    def test_drawer_never_computes_trading_facts(self) -> None:
        for name in ASSISTANT_FILES:
            source = (UI_ROOT / "assistant" / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                for forbidden in ("position_qty =", "pnl =", "exposure =", "RiskGate", "MakerPolicy"):
                    self.assertNotIn(forbidden, source)
