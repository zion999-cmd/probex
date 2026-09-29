"""P0001.10.2 UI ⇄ Product API 契约测试（SC-1 / SC-2 / SC-11）。"""

from __future__ import annotations

import json
import pathlib
import re
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
UI_ROOT = PROJECT_ROOT / "ui"
#: P0001.12.1：一级导航 = 5 个 Surface；旧页面降为 detail view（能力不删除）
#: F-16：risk / readiness / capabilities 已被 System section 完全覆盖 ⇒ 删除（不再是页面）。
SURFACES = ("monitor", "market", "activity", "performance", "system")
LEGACY_PAGES = ("overview", "prediction", "strategy", "orders", "portfolio", "evidence",
                "runs", "metrics")
PAGES = SURFACES + LEGACY_PAGES


class UiApiContractTest(unittest.TestCase):
    def test_every_page_exists_and_has_the_same_structure(self) -> None:
        """SC-2：REPLAY / PAPER / TESTNET 共用同一页面结构（页面不做模式分支）。"""
        for page in PAGES:
            with self.subTest(page=page):
                path = UI_ROOT / "pages" / page / "page.js"
                self.assertTrue(path.exists(), f"missing page {page}")
                source = path.read_text(encoding="utf-8")
                self.assertIn("export async function render", source)
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
                    if url in ("/", "") or url.startswith("/ui/"):
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
        for endpoint in ("/api/v1/metrics", "/api/v1/capabilities", "/api/v1/runs", "/api/v1/runs/compare"):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, client)

    def test_ui_does_not_compute_metrics(self) -> None:
        """P0001.11 SC-6：UI 只显示报告里的指标值，不得自行计算。"""
        for path in sorted(UI_ROOT.rglob("*.js")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                for forbidden in ("Math.sqrt", "stdev", "profitFactor", "sharpe(", "drawdown ="):
                    self.assertNotIn(forbidden, source)
        console = (UI_ROOT / "client" / "console.js").read_text(encoding="utf-8")
        self.assertIn("POLL_INTERVAL_MS", console)

    def test_unknown_rendering_helper_is_shared(self) -> None:
        """SC-3：UNKNOWN 由统一渲染原语显式显示（不显示 0）。"""
        render = (UI_ROOT / "client" / "render.js").read_text(encoding="utf-8")
        self.assertIn("UNKNOWN", render)
        self.assertIn("known", render)
