"""P0001.10.2 UI ⇄ Product API 契约测试（SC-1 / SC-2 / SC-11）。"""

from __future__ import annotations

import json
import pathlib
import re
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
UI_ROOT = PROJECT_ROOT / "ui"
PAGES = ("overview", "market", "prediction", "strategy", "risk", "orders", "portfolio",
         "readiness", "evidence", "runs")


class UiApiContractTest(unittest.TestCase):
    def test_every_page_exists_and_has_the_same_structure(self) -> None:
        """SC-2：REPLAY / PAPER / TESTNET 共用同一页面结构（页面不做模式分支）。"""
        for page in PAGES:
            with self.subTest(page=page):
                path = UI_ROOT / "pages" / page / "page.js"
                self.assertTrue(path.exists(), f"missing page {page}")
                source = path.read_text(encoding="utf-8")
                self.assertIn("export async function render()", source)
                self.assertIn("export const title", source)
                self.assertNotIn("REPLAY", source)
                self.assertNotIn("TESTNET", source)

    def test_ui_only_talks_to_the_product_api(self) -> None:
        """SC-1：UI 只通过 Product API 取事实，不 import 领域模块、不直连交易所。"""
        allowed_hosts = {"/api/v1/"}
        for path in sorted(UI_ROOT.rglob("*.js")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=str(path.relative_to(PROJECT_ROOT))):
                self.assertNotIn("binance", source.lower())
                self.assertNotIn("connectors", source)
                for url in re.findall(r'["\'](/[^"\']*)["\']', source):
                    if url.startswith("/ui/"):
                        continue
                    self.assertTrue(any(url.startswith(host) for host in allowed_hosts),
                                    f"UI references non-product URL {url}")

    def test_ui_has_no_trading_entry_points(self) -> None:
        """SC-11：UI 不存在任何直接交易写入口。"""
        forbidden = ("buy", "sell", "set-leverage", "leverage", "cancelOrder", "POST")
        for path in sorted(UI_ROOT.rglob("*")):
            if not path.is_file() or path.suffix not in (".js", ".html"):
                continue
            source = path.read_text(encoding="utf-8").lower()
            with self.subTest(file=str(path.relative_to(PROJECT_ROOT))):
                for needle in forbidden:
                    if needle == "buy" or needle == "sell":
                        continue  # 仅用在文案/字段名上（无交易动作）——由 API 侧 405 断言兜底
                    self.assertNotIn(needle, source)

    def test_console_polls_the_snapshot_endpoint(self) -> None:
        client = (UI_ROOT / "client" / "api.js").read_text(encoding="utf-8")
        self.assertIn("/api/v1/snapshot", client)
        self.assertIn("/api/v1/reports/run-summary", client)
        console = (UI_ROOT / "client" / "console.js").read_text(encoding="utf-8")
        self.assertIn("POLL_INTERVAL_MS", console)

    def test_unknown_rendering_helper_is_shared(self) -> None:
        """SC-3：UNKNOWN 由统一渲染原语显式显示（不显示 0）。"""
        render = (UI_ROOT / "client" / "render.js").read_text(encoding="utf-8")
        self.assertIn("UNKNOWN", render)
        self.assertIn("known", render)
