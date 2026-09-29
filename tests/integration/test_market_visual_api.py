"""P0001.12 集成：工作台 API（SC-1/2/7/8/9/10/11/12）+ RUN REVIEW + 只读边界。"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from api.server import create_server
from market.book.order_book import BookSide
from market.events.types import Venue
from market.replay.source import ReplaySource
from product.market_projection import BoundedMarketHistory, MarketProjectionConfig
from product.types import Fact, RuntimeMode
from storage.events.reader import JsonlEventReader
from storage.run_registry import JsonRunRegistry
from tests import scenarios
from tests.support import market_book, write_store
from tests.unit.test_product_snapshot import identity, service

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class MarketVisualApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="probex-workbench-"))
        self.store = self.tmp / "events.jsonl"
        self.registry = JsonRunRegistry(self.tmp / "runs")
        self.events = scenarios.reference_events()
        write_store(self.store, self.events)
        self.history = BoundedMarketHistory(capacity=200, run_id="rt-workbench")
        self.config = MarketProjectionConfig(window_ms=600_000, bucket_ms=1_000, max_points=50,
                                             price_levels=5)
        self._feed_history()
        self.service = service(market_history=lambda: self.history,
                               projection_config=lambda: self.config,
                               run_registry=lambda: self.registry)
        self.server = create_server(self.service, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _feed_history(self) -> None:
        """用真实 MarketBook 消费真实回放事件，并把事实喂进有界缓冲（不是重新推导）。"""
        from types import SimpleNamespace

        book = market_book()
        source = ReplaySource(JsonlEventReader(self.store))
        for event in source.iter_events():
            book.on_market_event(event)
            if event.event_type.value not in ("book_snapshot", "book_delta"):
                continue
            bids = [(level.price, level.size) for level in book.depth(BookSide.BID, 5)]
            asks = [(level.price, level.size) for level in book.depth(BookSide.ASK, 5)]
            self.history.feed_snapshot(SimpleNamespace(ts=event.exchange_ts, bids=bids, asks=asks))
            best_bid_level = book.best_bid()
            best_bid = best_bid_level.price if best_bid_level else None
            best_ask_level = book.best_ask()
            best_ask = best_ask_level.price if best_ask_level else None
            both = None not in (best_bid, best_ask)
            self.history.feed_state(SimpleNamespace(
                time=SimpleNamespace(as_of_exchange_ts=event.exchange_ts, event_ordinal=0),
                quality=SimpleNamespace(book_health="healthy", completeness=1.0),
                price=SimpleNamespace(best_bid=best_bid, best_ask=best_ask,
                                      mid=(best_bid + best_ask) / 2 if both else None,
                                      spread=(best_ask - best_bid) if both else None,
                                      microprice=None),
                depth=SimpleNamespace(l1_imbalance=None),
                flow=SimpleNamespace(ofi_1s=None),
            ))
        stamp = self.events[-1].exchange_ts
        self.history.feed_trade(SimpleNamespace(ts=stamp, price=100.0, quantity=0.5, aggressor="buy"))
        self.history.feed_health(SimpleNamespace(ts=self.events[0].exchange_ts, health="healthy", reason="",
                                                 market_generation=1))
        self.history.feed_decision(SimpleNamespace(ts=stamp, side="buy", action="place", price=100.0,
                                                   quantity=0.001, decision_id="maker:1:bid",
                                                   reason="passive quote"))
        self.history.feed_execution(SimpleNamespace(ts=stamp, client_order_id="probex-s1-1",
                                                    event="OrderAccepted", detail=""))
        self.book_snapshot_count = len(self.history.snapshots())

    def get(self, path: str) -> dict:
        with OPENER.open(f"{self.base}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_1_2_depth_endpoint_and_book_consistency(self) -> None:
        payload = self.get("/api/v1/market/depth")
        depth = payload["depth"]

        self.assertTrue(depth["cells"])
        self.assertTrue(depth["bid_series"] and depth["ask_series"])
        self.assertEqual(depth["bucket_ms"], self.config.bucket_ms)
        self.assertIn("NOT fill evidence", depth["notes"][0])
        # 与真实 MarketBook 一致：最后一个桶的 best bid/ask 等于缓冲里最后一个快照
        last_snapshot = self.history.snapshots()[-1]
        self.assertEqual(depth["bid_series"][-1][1], last_snapshot.bids[0][0])
        self.assertEqual(depth["ask_series"][-1][1], last_snapshot.asks[0][0])

    def test_7_timeline_features_come_from_owners(self) -> None:
        payload = self.get("/api/v1/market/timeline")
        timeline = payload["timeline"]

        self.assertTrue(timeline["points"])
        last = timeline["points"][-1]
        self.assertTrue(last["best_bid"]["known"])
        self.assertFalse(last["microprice"]["known"])  # 事实缺失 ⇒ UNKNOWN（不画 0）
        self.assertIsNone(last["microprice"]["value"])

    def test_8_11_health_and_unknown_are_explicit(self) -> None:
        health = self.get("/api/v1/market/health")["health"]
        self.assertEqual(health["segments"][0]["health"], "healthy")

        timeline = self.get("/api/v1/market/timeline")["timeline"]
        unknown_points = [p for p in timeline["points"] if not p["ofi"]["known"]]
        self.assertTrue(unknown_points)
        self.assertTrue(all(p["ofi"]["reason"] for p in unknown_points))

    def test_9_10_overlays_endpoint(self) -> None:
        overlays = self.get("/api/v1/market/overlays")["overlays"]

        self.assertEqual(overlays["decisions"][0]["decision_id"]["value"], "maker:1:bid")
        self.assertEqual(overlays["executions"][0]["client_order_id"], "probex-s1-1")

    def test_3_trades_endpoint_only_carries_trade_payloads(self) -> None:
        trades = self.get("/api/v1/market/trades")["trades"]
        self.assertEqual(len(trades["prints"]), 1)
        self.assertEqual(trades["prints"][0]["aggressor"], "buy")
        self.assertIn("never inferred", trades["notes"][0])

    def test_12_bounds_are_reported_and_enforced(self) -> None:
        payload = self.get("/api/v1/market/timeline")
        self.assertEqual(payload["bounds"]["max_points"], self.config.max_points)
        self.assertLessEqual(len(payload["timeline"]["points"]), self.config.max_points)
        self.assertEqual(payload["counts"]["capacity"], 200)

    def test_workbench_endpoints_are_503_without_wiring(self) -> None:
        server = create_server(service(), host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            for path in ("/api/v1/market/timeline", "/api/v1/market/depth"):
                with self.subTest(path=path):
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        OPENER.open(f"{base}{path}", timeout=5)
                    self.assertEqual(ctx.exception.code, 503)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_run_review_endpoint_requires_the_matching_run(self) -> None:
        payload = self.get("/api/v1/runs/rt-workbench/market")
        self.assertEqual(payload["run_id"], "rt-workbench")
        self.assertTrue(payload["timeline"]["points"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/v1/runs/other-run/market")
        self.assertEqual(ctx.exception.code, 404)
