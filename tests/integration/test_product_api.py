"""P0001.10 产品 API 集成测试（SC-9 / SC-10 / SC-11 + 只读边界）。"""

from __future__ import annotations

import json
import pathlib
import threading
import unittest
import urllib.error
import urllib.request

import shutil
import tempfile
from pathlib import Path

from api.server import create_server
from product.provenance import ConfigEntry, ConfigSource, build_config_snapshot
from product.types import SCHEMA_VERSION
from reports import build_run_summary
from reports.metrics import compute_metrics, metrics_payload
from tests.unit.test_product_snapshot import identity, service

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
GET_ENDPOINTS = ("/api/v1/status", "/api/v1/market", "/api/v1/prediction", "/api/v1/strategy",
                 "/api/v1/risk", "/api/v1/orders", "/api/v1/portfolio", "/api/v1/readiness",
                 "/api/v1/evidence", "/api/v1/blockers", "/api/v1/snapshot", "/api/v1/schema",
                 "/api/v1/metrics", "/api/v1/capabilities")
WRITE_ATTEMPTS = ("/api/v1/orders", "/api/v1/order", "/api/v1/buy", "/api/v1/sell", "/api/v1/position",
                  "/api/v1/leverage", "/api/v1/risk-limits")


#: 显式绕过一切代理（本机 loopback 不应经过任何 HTTP 代理）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ProductApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server(service(), host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def get(self, path: str) -> tuple[int, dict]:
        with OPENER.open(f"{self.base}{path}", timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_1_every_read_endpoint_returns_json_with_runtime_identity(self) -> None:
        for path in GET_ENDPOINTS:
            with self.subTest(path=path):
                status, payload = self.get(path)
                self.assertEqual(status, 200)
                if path == "/api/v1/schema":
                    self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
                    continue
                if path == "/api/v1/metrics":
                    self.assertTrue(payload["definitions"])
                    continue
                if path == "/api/v1/capabilities":
                    self.assertEqual(payload["api"]["write"], "unavailable_by_design")
                    continue
                self.assertIn("runtime", payload)
                self.assertEqual(payload["runtime"]["mode"], "PAPER")
                self.assertEqual(payload["runtime"]["symbol"], "BTCUSDT")

    def test_snapshot_matches_the_read_model(self) -> None:
        status, payload = self.get("/api/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["market"]["best_bid"]["value"], 60_000.0)
        # 已知的 0.0 与 UNKNOWN 必须可区分（0 是事实，UNKNOWN 不是 0）
        self.assertEqual(payload["portfolio"]["position_qty"], {"known": True, "value": 0.0, "reason": None})
        self.assertEqual(payload["risk"]["drawdown"], {"known": False, "value": None, "reason": "not_provided"})
        self.assertEqual(payload["readiness"]["reasons"], ["PRIVATE_LATENCY_UNOBSERVED"])

    def test_orders_endpoint_exposes_decision_traceability(self) -> None:
        _, payload = self.get("/api/v1/orders")
        orders = payload["execution"]["active_orders"]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["client_order_id"], "probex-s1-000001")
        # P0001.15 §15/SC-26：decision 关联来自订单自身的 canonical correlation
        self.assertEqual(orders[0]["decision_id"]["value"], "d-abc-2100")
        self.assertEqual(orders[0]["instrument_id"]["value"], "paper:BTCUSDT")
        self.assertEqual(orders[0]["venue_id"]["value"], "paper")

    def test_10_no_write_endpoint_exists(self) -> None:
        """SC-10：UI/API 没有任何绕过 RiskGate 的交易写入口。"""
        for path in WRITE_ATTEMPTS:
            with self.subTest(path=path):
                request = urllib.request.Request(f"{self.base}{path}", method="POST", data=b"{}")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    OPENER.open(request, timeout=5)
                self.assertIn(ctx.exception.code, (404, 405))

    def test_runtime_stop_is_reserved_not_implemented(self) -> None:
        request = urllib.request.Request(f"{self.base}/api/v1/runtime/stop", method="POST", data=b"{}")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            OPENER.open(request, timeout=5)
        self.assertEqual(ctx.exception.code, 501)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(body["error"], "not_implemented")

    def test_unknown_endpoint_is_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/v1/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_ui_console_is_served(self) -> None:
        """UI 由同一只读 server 提供（无 build step）；数据入口在 client 模块里。"""
        with OPENER.open(f"{self.base}/", timeout=5) as response:
            shell = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
        self.assertIn("Probex", shell)
        self.assertIn("/ui/client/console.js", shell)
        with OPENER.open(f"{self.base}/ui/client/api.js", timeout=5) as response:
            client = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
        self.assertIn("/api/v1/snapshot", client)
        self.assertIn("/api/v1/reports/run-summary", client)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/ui/../CLAUDE.md")
        self.assertEqual(ctx.exception.code, 404)

    def test_11_product_layer_never_touches_the_binance_execution_client(self) -> None:
        """SC-11：产品层不得 import Binance execution REST client，也不得依赖 connectors。"""
        for directory in ("product", "api"):
            for path in sorted((PROJECT_ROOT / directory).rglob("*.py")):
                source = path.read_text(encoding="utf-8")
                with self.subTest(module=str(path.relative_to(PROJECT_ROOT))):
                    self.assertNotIn("binance.execution", source)
                    self.assertNotIn("connectors", source)
                    if directory == "product":
                        self.assertNotIn("urllib", source)
                        self.assertNotIn("http.client", source)


class ProductOperationsApiTest(unittest.TestCase):
    """P0001.11：capabilities / metrics / blockers / run registry 端点。"""

    @classmethod
    def setUpClass(cls) -> None:
        from product.types import Fact
        from storage.run_registry import JsonRunRegistry

        cls.root = Path(tempfile.mkdtemp(prefix="probex-api-runs-"))
        cls.registry = JsonRunRegistry(cls.root)
        config = build_config_snapshot(
            config_id="cfg-api",
            entries=[ConfigEntry(name="symbol", source=ConfigSource.CONSTRUCTOR, value=Fact.of("BTCUSDT"))],
            created_at=1_000)
        cls.registry.start(runtime=identity(), run_id="run-api-1", now_ms=1_000, config=config)
        cls.registry.finalize(run_id="run-api-1", ended_at=9_000,
                              summary={"run": {"run_id": "run-api-1"},
                                       "metrics": metrics_payload(compute_metrics(
                                           owner_facts={"net_pnl": 1.25, "fees": 0.02}))})
        cls.summary = build_run_summary(identity=identity(), run_id="run-api-1", started_at=1_000,
                                        final_position=0.0, config_id="cfg-api",
                                        metrics_payload=metrics_payload(compute_metrics(
                                            owner_facts={"net_pnl": 1.25, "fees": 0.02})))
        cls.service = service(config_snapshot=lambda: config, run_registry=lambda: cls.registry,
                              run_summary=lambda: cls.summary,
                              orchestrator_notes=lambda: ("reconciling:pairing",))
        cls.server = create_server(cls.service, host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.root, ignore_errors=True)

    def get(self, path: str) -> tuple[int, dict]:
        with OPENER.open(f"{self.base}{path}", timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_schema_version_is_bumped(self) -> None:
        """P0001.11 → 2；P0001.12.2（G1–G5）→ 3；closure Slice 4（F-08 trace 字段）→ 4。"""
        _, payload = self.get("/api/v1/snapshot")
        self.assertEqual(payload["schema_version"], "5")
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)

    def test_capabilities_endpoint_reports_read_only_by_design(self) -> None:
        status, payload = self.get("/api/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(payload["api"]["write"], "unavailable_by_design")
        self.assertIn("/api/v1/snapshot", payload["api"]["read"])
        self.assertIn("place_order", payload["unavailable_actions"])
        self.assertEqual(payload["cli"]["exit_codes"]["20"], "blocked by policy or readiness")

    def test_metrics_endpoint_serves_definitions_ui_must_not_compute(self) -> None:
        _, payload = self.get("/api/v1/metrics")
        names = {item["name"] for item in payload["definitions"]}
        self.assertIn("sharpe", names)
        self.assertIn("run_mdd", names)
        for item in payload["definitions"]:
            self.assertTrue(item["formula"])
            self.assertTrue(item["unknown_condition"])

    def test_blockers_endpoint_returns_unified_projection(self) -> None:
        _, payload = self.get("/api/v1/blockers")
        blockers = payload["blockers"]
        self.assertTrue(blockers)
        for blocker in blockers:
            self.assertIn(blocker["severity"], ("BLOCKING", "DEGRADED", "INFO"))
            self.assertTrue(blocker["owner"])
            self.assertTrue(blocker["reason_code"])
            self.assertTrue(blocker["source_ref"])
        self.assertTrue(any(b["owner"] == "READINESS" for b in blockers))
        self.assertTrue(any(b["owner"] == "ORCHESTRATOR" for b in blockers))

    def test_config_provenance_is_exposed_without_secret_values(self) -> None:
        _, payload = self.get("/api/v1/snapshot")
        config = payload["config"]
        self.assertEqual(config["config_id"]["value"], "cfg-api")
        self.assertTrue(str(config["fingerprint"]["value"]).startswith("sha256:"))
        for entry in config["entries"]:
            self.assertNotIn("secret_value", entry)

    def test_run_registry_endpoints(self) -> None:
        status, listing = self.get("/api/v1/runs")
        self.assertEqual(status, 200)
        self.assertEqual([run["run_id"] for run in listing["runs"]], ["run-api-1"])
        self.assertEqual(listing["runs"][0]["status"], "COMPLETED")

        _, single = self.get("/api/v1/runs/run-api-1")
        self.assertEqual(single["run"]["run_id"], "run-api-1")
        self.assertEqual(single["run"]["config_id"]["value"], "cfg-api")

        _, compare = self.get("/api/v1/runs/compare?left=run-api-1&right=run-api-1")
        metrics = {item["name"]: item for item in compare["comparison"]["metrics"]}
        self.assertIn("net_pnl", metrics)
        self.assertEqual(metrics["net_pnl"]["delta"]["value"], 0.0)

    def test_unknown_run_is_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/v1/runs/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_missing_run_registry_is_503_not_empty_list(self) -> None:
        server = create_server(service(), host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                OPENER.open(f"{base}/api/v1/runs", timeout=5)
            self.assertEqual(ctx.exception.code, 503)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
