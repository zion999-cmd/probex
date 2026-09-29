"""P0001.12.2 集成：G1–G5 的 API 暴露（只读、有界、UNKNOWN 语义）。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

from api.server import create_server
from product.account_timeline import AccountSample, BoundedAccountTimeline
from product.market_projection import MarketProjectionConfig
from product.types import Fact
from tests.unit.test_product_snapshot import service

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SurfaceCapabilityApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.buffer = BoundedAccountTimeline(capacity=50, run_id="rt-1")
        for index in range(5):
            self.buffer.feed(AccountSample(ts=index * 1_000, equity=1_000.0 + index, balance=9_000.0,
                                           position_qty=0.0, exposure_total=50.0 + index))
        self.config = MarketProjectionConfig(window_ms=600_000, bucket_ms=1_000, max_points=4, price_levels=5)
        self.fill = SimpleNamespace(client_order_id="probex-s1-1", ts=2_000, price=82_000.0,
                                    quantity=0.0007, fee=0.01, trade_id="T-1")
        self.order = SimpleNamespace(client_order_id="probex-s1-1", symbol="BTCUSDT", side="buy",
                                     status="OPEN", price=82_000.0, quantity=0.0007,
                                     filled_quantity=0.0, reduce_only=False, created_at=1, updated_at=2)
        self.authority = SimpleNamespace(kind="NORMAL", issued_at_ms=1_000, expires_at_ms=31_000,
                                         recovery_generation="RecoveryGeneration(0, 1)",
                                         market_generation=7)
        self.server = create_server(self._service(), host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _service(self):
        return service(
            fills=lambda: (self.fill,), recent_fill_limit=10,
            raw_fact_lookup=lambda kind, identity: {"order": self.order, "fill": self.fill}.get(kind)
            if identity == "probex-s1-1" else None,
            account_timeline=lambda: self.buffer, projection_config=lambda: self.config,
            prediction_provider_status=lambda: "BACKING_OFF",
            accounting_health=lambda: SimpleNamespace(value="healthy"),
            authority=lambda: self.authority,
        )

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)

    def get(self, path: str) -> dict:
        with OPENER.open(f"{self.base}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_g1_fills_are_in_the_snapshot_and_trace(self) -> None:
        snapshot = self.get("/api/v1/snapshot")
        fills = snapshot["execution"]["recent_fills"]
        stages = [entry["stage"] for entry in snapshot["evidence"]["trace"]]

        self.assertEqual(fills[0]["client_order_id"]["value"], "probex-s1-1")
        self.assertEqual(fills[0]["price"]["value"], 82_000.0)
        self.assertEqual(snapshot["execution"]["recent_fill_limit"], 10)
        self.assertIn("fill", stages)

    def test_g2_facts_endpoint(self) -> None:
        fact = self.get("/api/v1/facts/order/probex-s1-1")["fact"]
        self.assertTrue(fact["available"])
        self.assertEqual(dict((name, value) for name, value in fact["facts"])["symbol"]["value"], "BTCUSDT")

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/v1/facts/order/does-not-exist")
        self.assertEqual(ctx.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/v1/facts/bogus/x")
        self.assertEqual(ctx.exception.code, 400)

    def test_g3_account_timeline_endpoint_is_bounded(self) -> None:
        payload = self.get("/api/v1/portfolio/timeline")
        timeline = payload["timeline"]

        self.assertLessEqual(len(timeline["points"]), self.config.max_points)
        self.assertTrue(timeline["truncated"])
        self.assertEqual(payload["bounds"]["max_points"], self.config.max_points)
        self.assertEqual(payload["run_id"], "rt-1")

    def test_g3_unwired_timeline_is_503(self) -> None:
        server = create_server(service(), host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                OPENER.open(f"{base}/api/v1/portfolio/timeline", timeout=5)
            self.assertEqual(ctx.exception.code, 503)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_g4_health_facts_are_in_the_snapshot(self) -> None:
        health = self.get("/api/v1/snapshot")["health"]

        self.assertEqual(health["prediction_provider"]["value"], "BACKING_OFF")
        self.assertEqual(health["accounting"]["value"], "healthy")

    def test_g5_full_authority_facts_are_in_readiness(self) -> None:
        readiness = self.get("/api/v1/snapshot")["readiness"]

        self.assertEqual(readiness["authority_kind"]["value"], "NORMAL")
        self.assertEqual(readiness["authority_expires_at_ms"]["value"], 31_000)
        self.assertEqual(readiness["authority_market_generation"]["value"], 7)
        self.assertEqual(readiness["authority_recovery_generation"]["value"], "RecoveryGeneration(0, 1)")

    def test_schema_version_is_four(self) -> None:
        # closure Slice 4：F-08 给 TraceEntry 增加 ts / identity_kind / latency_ms ⇒ 4
        self.assertEqual(self.get("/api/v1/snapshot")["schema_version"], "5")

    def test_no_new_write_path_was_introduced(self) -> None:
        """G1–G5 全是只读：任何写尝试仍然 405（除 replay control）。"""
        for path in ("/api/v1/facts/order/probex-s1-1", "/api/v1/portfolio/timeline"):
            with self.subTest(path=path):
                request = urllib.request.Request(f"{self.base}{path}", method="POST", data=b"{}")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    OPENER.open(request, timeout=5)
                self.assertEqual(ctx.exception.code, 405)
