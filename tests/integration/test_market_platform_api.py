"""P0017.1 Batch 4：多标的市场平台只读 API。"""

from __future__ import annotations

import json
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from api.server import create_server
from connectors.binance.market_data.universe import (
    Universe,
    UniverseSymbol,
    parse_universe,
)
from product.market_platform import MarketPlatform
from product.service import ProductService
from tests.support import BASE_TS


def _exchange_info():
    return {"symbols": [
        {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL",
         "baseAsset": "BTC", "quoteAsset": "USDT", "marginAsset": "USDT"},
        {"symbol": "ETHUSDT", "status": "TRADING", "contractType": "PERPETUAL",
         "baseAsset": "ETH", "quoteAsset": "USDT", "marginAsset": "USDT"},
    ]}


class _CollectorStub:
    symbols = ("BTCUSDT", "ETHUSDT")

    def subscriptions(self):
        from connectors.binance.market_data.multi_collector import Subscription
        return (
            Subscription("BTCUSDT", True, True, 100, 10, 0, None, BASE_TS, 1),
            Subscription("ETHUSDT", True, False, 0, 0, 0, "no data", None, 1),
        )


def _service(tmp: Path, *, wired: bool):
    universe = parse_universe(_exchange_info())
    if wired:
        from storage.market_dataset import MarketDatasetReader
        platform = MarketPlatform(
            universe_provider=lambda: universe.view(),
            collector_provider=lambda: _CollectorStub(),
            dataset_root=tmp,
            reader_factory=lambda root, *, venue, symbol, kind:
                MarketDatasetReader(root, venue=venue, symbol=symbol, kind=kind))
    else:
        platform = None
    return ProductService(identity=None, market_platform=lambda: platform)


import unittest  # noqa: E402


class MarketPlatformApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mkt-api-"))
        self.server = create_server(_service(self.tmp, wired=True), host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()

    def get_json(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read())

    def test_universe_endpoint(self) -> None:
        status, body = self.get_json("/api/v1/market/universe")
        self.assertEqual(status, 200)
        self.assertEqual(body["discovered_count"], 2)
        self.assertEqual([s["symbol"] for s in body["symbols"]], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(body["quote_assets"], ["USDT"])

    def test_subscriptions_endpoint(self) -> None:
        _, body = self.get_json("/api/v1/market/subscriptions")
        symbols = {s["symbol"]: s for s in body["symbols"]}
        self.assertEqual(symbols["BTCUSDT"]["status"], "HEALTHY")
        self.assertEqual(symbols["ETHUSDT"]["status"], "SUBSCRIBING")

    def test_symbol_status_endpoint(self) -> None:
        _, body = self.get_json("/api/v1/market/symbols/BTCUSDT/status")
        self.assertEqual(body["symbol"], "BTCUSDT")
        self.assertEqual(body["status"], "HEALTHY")

    def test_dataset_empty_is_explicit(self) -> None:
        _, body = self.get_json("/api/v1/market/dataset/trade/BTCUSDT?limit=10")
        self.assertEqual(body["records"], [])
        self.assertIn("no recorded data", body["note"])

    def test_dataset_returns_written_records(self) -> None:
        from storage.market_dataset import MarketDatasetWriter
        with MarketDatasetWriter(self.tmp, venue="binance", symbol="BTCUSDT",
                                   kind="trade") as w:
            w.append(exchange_ts=BASE_TS + 1, payload={"price": 1.0})
        _, body = self.get_json("/api/v1/market/dataset/trade/BTCUSDT")
        self.assertEqual(len(body["records"]), 1)
        self.assertEqual(body["partitions"][0]["rows"], 1)

    def test_bad_dataset_path_400(self) -> None:
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as ctx:
            self.get_json("/api/v1/market/dataset/bogus/BTCUSDT")
        self.assertEqual(ctx.exception.code, 400)


class MarketPlatformUnwiredApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mkt-api2-"))
        self.server = create_server(_service(self.tmp, wired=False), host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()

    def test_unwired_returns_503_not_fake_data(self) -> None:
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as ctx:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/v1/market/universe", timeout=5)
        self.assertEqual(ctx.exception.code, 503)


if __name__ == "__main__":
    unittest.main()
