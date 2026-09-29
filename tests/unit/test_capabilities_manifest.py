"""P0001.11 §5 / 裁决 E：Capability Manifest 必须由注册表生成，且写能力固定不可用。"""

from __future__ import annotations

import pathlib
import unittest

from api.capabilities import RESERVED_ENDPOINTS, UNAVAILABLE_ACTIONS, WRITE_UNAVAILABLE, \
    build_capabilities_manifest
from api.routes import ROUTES
from cli.main import COMMAND_SPEC, EXIT_CODES
from product.types import SCHEMA_VERSION

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


class CapabilitiesManifestTest(unittest.TestCase):
    def manifest(self) -> dict:
        return build_capabilities_manifest(schema_version=SCHEMA_VERSION,
                                           api_read=tuple(sorted(ROUTES)),
                                           cli_commands=COMMAND_SPEC, exit_codes=EXIT_CODES)

    def test_write_capability_is_unavailable_by_design(self) -> None:
        manifest = self.manifest()
        self.assertEqual(manifest["api"]["write"], WRITE_UNAVAILABLE)
        self.assertEqual(manifest["api"]["write"], "unavailable_by_design")

    def test_read_paths_are_generated_from_the_route_registry(self) -> None:
        manifest = self.manifest()
        self.assertEqual(sorted(manifest["api"]["read"]), sorted(ROUTES))

    def test_cli_commands_and_exit_codes_are_generated_from_the_registry(self) -> None:
        manifest = self.manifest()
        self.assertEqual(manifest["cli"]["commands"], dict(sorted(COMMAND_SPEC.items())))
        self.assertEqual(manifest["cli"]["exit_codes"], dict(sorted(EXIT_CODES.items())))
        self.assertEqual(EXIT_CODES["20"], "blocked by policy or readiness")

    def test_no_write_command_exists_in_the_registry(self) -> None:
        for forbidden in ("buy", "sell", "order", "cancel", "set-risk", "set-leverage"):
            self.assertNotIn(forbidden, COMMAND_SPEC)

    def test_reserved_endpoints_are_declared(self) -> None:
        manifest = self.manifest()
        self.assertEqual(manifest["api"]["reserved"], sorted(RESERVED_ENDPOINTS))
        self.assertIn("/api/v1/runtime/stop", RESERVED_ENDPOINTS)

    def test_unavailable_actions_are_enumerated(self) -> None:
        manifest = self.manifest()
        self.assertEqual(manifest["unavailable_actions"], list(UNAVAILABLE_ACTIONS))
        for action in ("place_order", "set_leverage", "set_risk_limits"):
            self.assertIn(action, UNAVAILABLE_ACTIONS)

    def test_api_has_no_write_handlers_at_all(self) -> None:
        """结构性固定：任何非 GET 都没有实现（除预留 stop 返回 501）。"""
        source = (PROJECT_ROOT / "api" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("def do_PUT_write", source)
        self.assertIn("def do_POST", source)
        self.assertIn("read_only_api", source)
        for forbidden in ("submit_post_only_limit", "cancel_order(", "place_order"):
            self.assertNotIn(forbidden, source)

    def test_server_derives_manifest_instead_of_hardcoding_paths(self) -> None:
        source = (PROJECT_ROOT / "api" / "server.py").read_text(encoding="utf-8")
        self.assertIn("build_capabilities_manifest", source)
        self.assertIn("COMMAND_SPEC", source)


class ProductLayerOwnershipTest(unittest.TestCase):
    """SC-9：Product 层仍未成为 Accounting / Risk / Execution Owner。"""

    FORBIDDEN_ROOTS = ("execution", "risk", "portfolio", "strategy", "connectors", "live", "storage")

    def test_product_layer_does_not_import_trading_owners(self) -> None:
        for path in sorted((PROJECT_ROOT / "product").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                for line in source.splitlines():
                    stripped = line.strip()
                    if not (stripped.startswith("from ") or stripped.startswith("import ")):
                        continue
                    for root in self.FORBIDDEN_ROOTS:
                        self.assertFalse(stripped.startswith(f"from {root}.") or stripped.startswith(f"import {root}"),
                                         f"{path.name} must not import {root}")

    def test_product_layer_reads_only_read_only_facts(self) -> None:
        source = (PROJECT_ROOT / "product" / "service.py").read_text(encoding="utf-8")
        for forbidden in ("submit(", "cancel(", ".add(", ".mark_"):
            self.assertNotIn(forbidden, source)
