"""F-16 Cross-Surface Navigation：Python 契约 + UI 镜像一致 + 无死页面。"""

from __future__ import annotations

import pathlib
import re
import unittest

from product.navigation import (BLOCKER_SECTION, DETAIL_ROUTES, NavigationError, blocker_route,
                                detail_route_keys, entity_route, market_point_route, surface_route)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
UI_ROOT = PROJECT_ROOT / "ui"


class RouteContractTest(unittest.TestCase):
    def test_surface_routes_are_canonical(self) -> None:
        target = surface_route("activity", detail="evidence", identity="probex-s1-1")
        self.assertEqual(target.target, "#/activity/evidence/probex-s1-1")
        self.assertEqual(target.surface, "activity")
        self.assertEqual(target.identity, "probex-s1-1")

    def test_unknown_surface_or_detail_is_refused(self) -> None:
        with self.assertRaises(NavigationError):
            surface_route("nope")
        with self.assertRaises(NavigationError):
            surface_route("system", detail="nope")

    def test_entity_routes_use_canonical_ids(self) -> None:
        self.assertEqual(entity_route("order", "probex-1").target, "#/activity/evidence/probex-1")
        self.assertEqual(entity_route("fill", "T-1").target, "#/activity/evidence/T-1")
        self.assertEqual(entity_route("run", "run-9").target, "#/performance/runs/run-9")
        with self.assertRaises(NavigationError):
            entity_route("order", "")
        with self.assertRaises(NavigationError):
            entity_route("nope", "x")

    def test_blocker_routes_map_to_system_sections(self) -> None:
        self.assertEqual(blocker_route("READINESS").target, "#/system/readiness")
        self.assertEqual(blocker_route("VENUE").target, "#/system/execution")
        self.assertEqual(blocker_route("MARKET").target, "#/system/health")
        for owner in BLOCKER_SECTION:
            self.assertTrue(blocker_route(owner).target.startswith("#/system/"))

    def test_market_point_route_uses_the_timestamp(self) -> None:
        self.assertEqual(market_point_route(1_700_000_000_110).target, "#/market/live/1700000000110")
        with self.assertRaises(NavigationError):
            market_point_route("not-a-ts")


def _js_detail_route_keys() -> set[str]:
    source = (UI_ROOT / "app" / "navigation.js").read_text(encoding="utf-8")
    block = source.split("export const DETAIL_ROUTES = {", 1)[1].split("};", 1)[0]
    keys: set[str] = set()
    for surface, body in re.findall(r"(\w+): \{([^}]*)\}", block):
        for quoted, bare in re.findall(r'"([a-z-]+)"\s*:|([a-z][a-z-]*)\s*:', body):
            keys.add(f"{surface}/{quoted or bare}")
    return keys


def _js_detail_page_modules() -> set[str]:
    source = (UI_ROOT / "app" / "navigation.js").read_text(encoding="utf-8")
    block = source.split("export const DETAIL_PAGE_MODULES = {", 1)[1].split("};", 1)[0]
    return set(re.findall(r'"([a-z-]+/[a-z-]+)":', block))


class UiMirrorTest(unittest.TestCase):
    def test_ui_detail_routes_match_the_python_contract(self) -> None:
        self.assertEqual(_js_detail_route_keys(), set(detail_route_keys()))

    def test_every_legacy_detail_page_is_reachable(self) -> None:
        """F-16：不允许\"文件存在但产品没有入口\"的死页面。"""
        surfaces = {"monitor", "market", "activity", "performance", "system"}
        page_dirs = {path.parent.name for path in (UI_ROOT / "pages").glob("*/page.js")}
        legacy = page_dirs - surfaces
        reachable = _js_detail_page_modules()
        routed = {key.split("/", 1)[1] for key in reachable if key.startswith(("monitor/", "activity/",
                                                                              "performance/"))}
        for page in sorted(legacy):
            with self.subTest(page=page):
                self.assertIn(page, routed, f"{page} is a dead page (no detail route)")

    def test_blocker_section_mapping_matches(self) -> None:
        source = (UI_ROOT / "app" / "navigation.js").read_text(encoding="utf-8")
        block = source.split("export const BLOCKER_SECTION = {", 1)[1].split("};", 1)[0]
        mapping = dict(re.findall(r"(\w+): \"([a-z-]+)\"", block))
        self.assertEqual(mapping, BLOCKER_SECTION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
