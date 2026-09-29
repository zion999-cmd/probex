"""P0001.10 产品 API 集成测试（SC-9 / SC-10 / SC-11 + 只读边界）。"""

from __future__ import annotations

import json
import pathlib
import threading
import unittest
import urllib.error
import urllib.request

from api.server import create_server
from product.types import SCHEMA_VERSION
from tests.unit.test_product_snapshot import service

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
GET_ENDPOINTS = ("/api/v1/status", "/api/v1/market", "/api/v1/prediction", "/api/v1/strategy",
                 "/api/v1/risk", "/api/v1/orders", "/api/v1/portfolio", "/api/v1/readiness",
                 "/api/v1/evidence", "/api/v1/snapshot", "/api/v1/schema")
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
        self.assertEqual(orders[0]["decision_id"]["value"], "maker:2100:bid")

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
        self.assertIn("Probex console", shell)
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
