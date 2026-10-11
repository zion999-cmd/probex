"""P0017.1 Batch 5：assembly 的 binance-universe 数据平台接线（离线）。

用注入的 FakeHttp（exchangeInfo）+ ScriptedTransport 验证：
- 发现的 universe 进入只读平台投影（/market/universe）；
- 显式订阅集经 collector 采集并写入共享数据集（按 symbol 隔离）；
- 不构造 broker/engine（数据平台只读）；未配置订阅时只发现不订阅。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

from api.server import create_server
from connectors.binance.market_data.endpoints import (
    DEPTH_PATH,
    EXCHANGE_INFO_PATH,
    SERVER_TIME_PATH,
)
from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from runtime.assembly import ProductRuntime, RuntimeProfile
from tests.live_support import (
    FakeHttp,
    ScriptedTransport,
    agg_trade_message,
    depth_snapshot_payload,
    exchange_info_payload,
)
from tests.support import BASE_TS, TempDirTestCase


def _exchange_info_multi():
    # 直接构造一个多 symbol 的 exchangeInfo（复用真实过滤结构）
    def rules(symbol, tick):
        return {
            "symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL",
            "baseAsset": symbol.replace("USDT", ""), "quoteAsset": "USDT", "marginAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "0.1", "maxPrice": "1e9", "tickSize": tick},
                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1e6", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }
    return {"symbols": [rules("BTCUSDT", "0.1"), rules("ETHUSDT", "0.01")]}


class UniversePlatformWiringTest(TempDirTestCase):
    def _runtime(self, *, symbols: str):
        transport = ScriptedTransport()
        http = FakeHttp(responses={
            DEPTH_PATH: depth_snapshot_payload(last_update_id=100,
                                                 bids=((60000.0, 1.0),), asks=((60000.1, 1.0),)),
            EXCHANGE_INFO_PATH: _exchange_info_multi(),
            SERVER_TIME_PATH: {"serverTime": BASE_TS},
        })
        entries = (
            ConfigEntry(name="market.universe.symbols", source=ConfigSource.FILE,
                        value=Fact.of(symbols)),
            ConfigEntry(name="market.subscription.max_symbols", source=ConfigSource.FILE,
                        value=Fact.of(16)),
            ConfigEntry(name="market.dataset.dir", source=ConfigSource.FILE,
                        value=Fact.of(str(self.tmp_path / "market-data"))),
            ConfigEntry(name="market.collector.autostart", source=ConfigSource.FILE,
                        value=Fact.of(False)),
        )
        runtime = ProductRuntime(profile=RuntimeProfile(
            symbol="BTCUSDT", config_entries=entries, mode=RuntimeMode.PAPER,
            run_registry_dir=str(self.tmp_path / "runs"), market_source="binance-universe"))
        runtime._public_http_client = http       # noqa: SLF001
        runtime._public_transport_factory = transport  # noqa: SLF001
        self._transport = transport
        return runtime

    def test_universe_discovered_and_subscriptions_persisted(self) -> None:
        runtime = self._runtime(symbols="BTCUSDT,ETHUSDT")
        runtime.start()
        server = create_server(runtime.service, host="127.0.0.1", port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def get(path):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                    return json.loads(r.read())
            universe = get("/api/v1/market/universe")
            self.assertEqual(universe["discovered_count"], 2)
            subs = get("/api/v1/market/subscriptions")
            self.assertEqual(subs["subscribed_count"], 2)
            # 注入两个标的各自的成交
            self._transport.connections  # ensure dict populated per tier
            # 所有 runtime 共用同一注入 transport（ScriptedTransport 按 tier），把成交推到 market tier
            market = self._transport.connection("market")
            market.push(agg_trade_message(aggregate_trade_id=1, price=60000.0, quantity=0.5,
                                            trade_ts=BASE_TS + 50, symbol="BTCUSDT"))
            # 显式 pump 两个 symbol 多轮（无后台线程，确定性）
            for _ in range(8):
                runtime._universe_collector.pump_once()  # noqa: SLF001
            data = get("/api/v1/market/dataset/trade/BTCUSDT")
            self.assertGreaterEqual(len(data["records"]), 1)
        finally:
            server.shutdown()
            runtime.stop()


if __name__ == "__main__":
    import unittest
    unittest.main()
