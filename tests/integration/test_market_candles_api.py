"""`/api/v1/market/candles` 真实运行时验收（REPLAY/PAPER + 有成交的 fixture）。"""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from runtime.assembly import FeedProfile, ProductRuntime, RuntimeProfile
from tests.ui.market_fixture import write_market_store

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
CAPACITY = 1000


class MarketCandlesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="probex-candles-"))
        store = self.tmp / "events.jsonl"
        write_market_store(store, hours=1, step_ms=60_000)      # 60 snapshots + 60 trades over 1h
        values = json.loads((PROJECT_ROOT / "profiles" / "trial-local.json").read_text(encoding="utf-8"))
        values["projection.history_capacity"] = CAPACITY
        values["projection.max_points"] = 200
        entries = tuple(ConfigEntry(name=str(k), source=ConfigSource.FILE, value=Fact.of(v))
                        for k, v in values.items())
        self.runtime = ProductRuntime(profile=RuntimeProfile(
            symbol="BTCUSDT", config_entries=entries, mode=RuntimeMode.REPLAY,
            run_registry_dir=str(self.tmp / "runs"),
            feed=FeedProfile(event_store=str(store), window_ms=3_600_000, bucket_ms=1_000,
                             max_points=200, price_levels=5, history_capacity=CAPACITY, view_depth=10)))
        self.runtime.start()
        provider = self.runtime._feed_provider  # noqa: SLF001
        deadline = time.time() + 30
        while time.time() < deadline and not provider.stats.get("completed"):
            time.sleep(0.05)
        self.server = self.runtime.create_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = self.runtime.server_url()

    def tearDown(self) -> None:
        self.runtime.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str) -> tuple[int, dict]:
        try:
            with OPENER.open(f"{self.base}{path}", timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_candles_are_aggregated_from_real_trades(self) -> None:
        status, payload = self._get("/api/v1/market/candles?interval=1m&limit=300")
        self.assertEqual(status, 200, payload)
        series = payload["candles"]
        self.assertEqual(series["source"], "trades")
        self.assertGreater(len(series["candles"]), 0)
        first = series["candles"][0]
        for key in ("ts", "open", "high", "low", "close", "volume", "source"):
            self.assertIn(key, first)
        self.assertLessEqual(first["low"], first["close"])
        self.assertGreaterEqual(first["high"], first["close"])
        self.assertGreater(first["volume"], 0)

    def test_timeframes_and_alignment(self) -> None:
        for interval, step in (("1m", 60_000), ("5m", 300_000), ("15m", 900_000), ("1h", 3_600_000)):
            with self.subTest(interval=interval):
                status, payload = self._get(f"/api/v1/market/candles?interval={interval}")
                self.assertEqual(status, 200)
                candles = payload["candles"]["candles"]
                self.assertGreater(len(candles), 0)
                for candle in candles:
                    self.assertEqual(candle["ts"] % step, 0)

    def test_invalid_params_are_refused(self) -> None:
        status, payload = self._get("/api/v1/market/candles?interval=2m")
        self.assertEqual(status, 400, payload)
        status, payload = self._get("/api/v1/market/candles?limit=100000")
        self.assertEqual(status, 400, payload)
        status, payload = self._get("/api/v1/market/candles?limit=abc")
        self.assertEqual(status, 400, payload)

    def test_series_are_bounded(self) -> None:
        status, payload = self._get("/api/v1/market/candles?interval=1m&limit=5")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(payload["candles"]["candles"]), 5)
        self.assertTrue(payload["candles"]["truncated"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
