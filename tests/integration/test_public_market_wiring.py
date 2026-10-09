"""全局补齐 Batch 3：产品入口接真实公网行情（G-A2 / G-B2）的离线集成测试。

用 `tests/live_support.py` 的脚本化传输/HTTP（与真实 Binance 报文同形）验证装配链路：

```
binance-public ⇒ LiveMarketDataRuntime（真实消息解析）⇒ PublicMarketPump（线程）
  ⇒ BoundedMarketHistory（states/snapshots/trades）⇒ Product Read Model（snapshot/API）
  ⇒ run 级 market/trades 事实持久化（既有 RunRegistry）
```

不触网、不需凭据；真实公网验收在当前网络被 Binance 451 geo-block（见 handoff，NOT RUN）。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from runtime.assembly import ProductRuntime, RuntimeProfile
from tests.live_support import (
    FakeHttp,
    ScriptedTransport,
    agg_trade_message,
    depth_update_message,
)
from tests.support import BASE_TS, TempDirTestCase

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: 注入 HTTP 响应路径（复用 live_support 约定）
from connectors.binance.market_data.endpoints import (  # noqa: E402
    DEPTH_PATH,
    EXCHANGE_INFO_PATH,
    SERVER_TIME_PATH,
)
from tests.live_support import (  # noqa: E402
    depth_snapshot_payload,
    exchange_info_payload,
)


class PublicMarketAssemblyTest(TempDirTestCase):
    def _runtime(self) -> ProductRuntime:
        transport = ScriptedTransport()
        http = FakeHttp(responses={
            DEPTH_PATH: depth_snapshot_payload(last_update_id=100,
                                               bids=((60000.0, 1.0),), asks=((60000.1, 1.0),)),
            EXCHANGE_INFO_PATH: exchange_info_payload(),
            SERVER_TIME_PATH: {"serverTime": BASE_TS},
        })
        entries = (ConfigEntry(name="projection.history_capacity", source=ConfigSource.FILE,
                               value=Fact.of(2_000)),
                   ConfigEntry(name="market.max_book_age_ms", source=ConfigSource.FILE,
                               value=Fact.of(5_000)))
        runtime = ProductRuntime(profile=RuntimeProfile(
            symbol="BTCUSDT", config_entries=entries, mode=RuntimeMode.PAPER,
            run_registry_dir=str(self.tmp_path / "runs"), market_source="binance-public"))
        # 注入缝（默认真实组件；测试内替换为脚本化的，零网络）
        runtime._public_transport_factory = transport           # noqa: SLF001
        runtime._public_http_client = http                      # noqa: SLF001
        self._transport = transport
        return runtime

    def test_real_public_messages_flow_into_product_and_persist(self) -> None:
        runtime = self._runtime()
        runtime.start()
        pump = runtime._public_pump                              # noqa: SLF001
        self.assertIsNotNone(pump)
        try:
            public = self._transport.connection("public")
            # 连续 futures 深度更新（pu 链：100 → 200 → 300）+ aggTrade
            public.push(depth_update_message(first_update_id=100, last_update_id=200,
                                             previous_update_id=100, bids=((60000.0, 1.0),),
                                             speed="100ms", exchange_ts=BASE_TS + 100))
            public.push(depth_update_message(first_update_id=200, last_update_id=300,
                                             previous_update_id=200, bids=((60000.0, 1.0),),
                                             speed="100ms", exchange_ts=BASE_TS + 200))
            market_conn = self._transport.connection("market")
            market_conn.push(agg_trade_message(aggregate_trade_id=1, price=60000.05, quantity=0.5,
                                               trade_ts=BASE_TS + 300))

            # 等 pump 线程消费
            deadline = time.time() + 10
            while time.time() < deadline:
                history = runtime._history                        # noqa: SLF001
                counts = history.counts
                if counts["trades"] >= 1 and counts["states"] >= 2:
                    break
                time.sleep(0.1)

            counts = runtime._history.counts                      # noqa: SLF001
            self.assertGreaterEqual(counts["states"], 2)
            self.assertGreaterEqual(counts["trades"], 1)

            # Product Read Model 看到真实事实
            snapshot = runtime.service.snapshot()
            mc = snapshot.market_connector_health
            self.assertEqual(mc.connection_state.value, "CONNECTED")
            self.assertIn("messages=", mc.detail.value)
            self.assertTrue(snapshot.venue.market_connector_id.known)

            # run 级事实已持久化（market + trades 两类）
            rid = runtime.run_id
            market_points, reason = runtime._registry.load_run_facts(rid, "market")  # noqa: SLF001
            trades, trade_reason = runtime._registry.load_run_facts(rid, "trades")  # noqa: SLF001
            self.assertIsNone(reason)
            self.assertGreaterEqual(len(market_points), 1)
            self.assertGreaterEqual(len(trades), 1)
            self.assertEqual(trades[0]["price"], 60000.05)
        finally:
            runtime.stop()

    def test_stop_closes_pump_and_connectors(self) -> None:
        runtime = self._runtime()
        runtime.start()
        time.sleep(0.5)
        status = runtime.stop()
        self.assertEqual(status.state.value, "STOPPED")
        self.assertIsNone(runtime._public_pump)                   # noqa: SLF001
