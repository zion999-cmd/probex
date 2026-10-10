"""默认免费行情源（Binance 公开 REST/WS）经统一适配器接入的离线集成测试。

用脚本化传输/HTTP（与真实 Binance 报文同形）验证默认路径：

```
binance-public ⇒ LiveMarketDataRuntime（真实消息解析）
  ⇒ BinancePublicSourceAdapter（MarketSourceAdapter）
  ⇒ PublicMarketPump ⇒ BoundedMarketHistory（MarketState）
  ⇒ Product Read Model + run 级持久化
```

不触网、不需凭据；真实公网验收在当前网络被 Binance 451 geo-block（NOT RUN，见 handoff）。
数据源扩展点（任意免费/付费源）见 `test_public_source_extension.py`。
"""

from __future__ import annotations

import time

from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from runtime.assembly import ProductRuntime, RuntimeProfile
from tests.live_support import (
    FakeHttp,
    ScriptedTransport,
    depth_update_message,
)
from tests.support import BASE_TS, TempDirTestCase

from connectors.binance.market_data.endpoints import (  # noqa: E402
    DEPTH_PATH,
    EXCHANGE_INFO_PATH,
    SERVER_TIME_PATH,
)
from tests.live_support import (  # noqa: E402
    depth_snapshot_payload,
    exchange_info_payload,
)


class BinancePublicAssemblyTest(TempDirTestCase):
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
        runtime._public_transport_factory = transport           # noqa: SLF001
        runtime._public_http_client = http                      # noqa: SLF001
        self._transport = transport
        return runtime

    def test_default_binance_public_source_produces_states_and_persists(self) -> None:
        runtime = self._runtime()
        runtime.start()
        pump = runtime._public_pump                              # noqa: SLF001
        self.assertIsNotNone(pump)
        self.assertEqual(pump.adapter.source_id, "binance-public")
        try:
            public = self._transport.connection("public")
            public.push(depth_update_message(first_update_id=100, last_update_id=200,
                                             previous_update_id=100, bids=((60000.0, 1.0),),
                                             speed="100ms", exchange_ts=BASE_TS + 100))
            public.push(depth_update_message(first_update_id=200, last_update_id=300,
                                             previous_update_id=200, bids=((60000.0, 1.0),),
                                             speed="100ms", exchange_ts=BASE_TS + 200))

            deadline = time.time() + 10
            while time.time() < deadline:
                if runtime._history.counts["states"] >= 2:       # noqa: SLF001
                    break
                time.sleep(0.1)
            self.assertGreaterEqual(runtime._history.counts["states"], 2)  # noqa: SLF001

            # 默认源是 Binance 适配器（connector health 真实）
            snapshot = runtime.service.snapshot()
            mc = snapshot.market_connector_health
            self.assertEqual(mc.connection_state.value, "CONNECTED")
            self.assertTrue(snapshot.runtime.data_timestamp.known)

            # run 级 market 持久化（既有 registry）
            rid = runtime.run_id
            points, reason = runtime._registry.load_run_facts(rid, "market")   # noqa: SLF001
            self.assertIsNone(reason)
            self.assertGreaterEqual(len(points), 1)
        finally:
            runtime.stop()

    def test_stop_closes_pump(self) -> None:
        runtime = self._runtime()
        runtime.start()
        time.sleep(0.4)
        status = runtime.stop()
        self.assertEqual(status.state.value, "STOPPED")
        self.assertIsNone(runtime._public_pump)                   # noqa: SLF001
