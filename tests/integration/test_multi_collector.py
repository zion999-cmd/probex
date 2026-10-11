"""P0017.1 Batch 3：多标的采集编排（离线，脚本化传输）。

验证：
- 每标的独立 runtime / FeatureEngine，A 的数据不串到 B；
- 单标的畸形报文不污染其他标的（故障隔离）；
- 订阅状态/时间戳按标的独立；订阅上限校验。
"""

from __future__ import annotations

import unittest

from connectors.binance.market_data.endpoints import (
    DEPTH_PATH,
    EXCHANGE_INFO_PATH,
    SERVER_TIME_PATH,
)
from connectors.binance.market_data.multi_collector import (
    CollectorConfig,
    MultiSymbolMarketCollector,
)
from connectors.binance.market_data.runtime import MarketDataFormatError
from tests.live_support import (
    FakeHttp,
    ScriptedTransport,
    depth_snapshot_payload,
    depth_update_message,
    exchange_info_payload,
)
from tests.support import BASE_TS


def _symbol_http(symbol: str) -> FakeHttp:
    return FakeHttp(responses={
        DEPTH_PATH: depth_snapshot_payload(last_update_id=100,
                                            bids=((60000.0, 1.0),), asks=((60000.1, 1.0),))
        if symbol == "BTCUSDT"
        else depth_snapshot_payload(last_update_id=100,
                                     bids=((3000.0, 5.0),), asks=((3000.1, 5.0),))
        if symbol == "ETHUSDT" else depth_snapshot_payload(last_update_id=100),
        EXCHANGE_INFO_PATH: exchange_info_payload(symbol=symbol, tick_size="0.01"
                                                   if symbol == "ETHUSDT" else "0.10"),
        SERVER_TIME_PATH: {"serverTime": BASE_TS},
    })


def _collector(symbols=("BTCUSDT", "ETHUSDT"), *, transports=None, on_batch=None):
    transports = transports or {}
    return MultiSymbolMarketCollector(
        CollectorConfig(symbols=symbols, pump_timeout_s=0.01, max_messages=16,
                         max_book_age_ms=5_000),
        clock=lambda: BASE_TS,
        http_client_factory=lambda base: _symbol_http(_factory_symbol[0]),
        transport_factory=None,
        on_batch=on_batch)


def _collector(symbols=("BTCUSDT", "ETHUSDT"), *, on_batch=None):
    return MultiSymbolMarketCollector(
        CollectorConfig(symbols=symbols, pump_timeout_s=0.01, max_messages=16,
                         max_book_age_ms=5_000),
        clock=lambda: BASE_TS,
        http_client_factory=lambda base: None,
        on_batch=on_batch)


# helper: a collector with explicit per-symbol runtime injection
from connectors.binance.market_data.runtime import LiveMarketDataRuntime, LiveMarketDataConfig  # noqa: E402
from connectors.binance.market_data.transport import ReconnectPolicy  # noqa: E402


def build_runtime(symbol, transport, http):
    return LiveMarketDataRuntime(
        config=LiveMarketDataConfig(
            symbol=symbol, depth_speed="100ms", mark_price_speed="1s", depth_limit=100,
            connect_timeout_s=1.0, read_timeout_s=0.01, snapshot_timeout_s=1.0,
            exchange_info_timeout_s=1.0,
            reconnect=ReconnectPolicy(max_attempts=1, base_backoff_ms=1, max_backoff_ms=1),
            resync_cooldown_ms=0, history_limit=32),
        http_client=http,
        transport_factory=tr_callable(transport),
        clock=lambda: BASE_TS,
        sleeper=lambda _s: None)


def tr_callable(transport: ScriptedTransport):
    return lambda url, timeout_s: transport(url, timeout_s=timeout_s)


class MultiCollectorIsolationTest(unittest.TestCase):
    def _wired(self, symbols=("BTCUSDT", "ETHUSDT")):
        transports = {s: ScriptedTransport() for s in symbols}
        https = {s: _symbol_http(s) for s in symbols}
        collector = _collector(symbols)
        for symbol in symbols:
            runtime = build_runtime(symbol, transports[symbol], https[symbol])
            runtime.load_trading_rules()
            runtime.connect()
            collector._runtimes[symbol] = runtime  # noqa: SLF001
        collector._connected = True              # noqa: SLF001
        return collector, transports, https

    def test_symbols_route_to_isolated_state(self) -> None:
        collector, transports, _ = self._wired()
        transports["BTCUSDT"].connection("public").push(
            depth_update_message(first_update_id=100, last_update_id=200, previous_update_id=100,
                                 bids=((60000.0, 1.0),), speed="100ms",
                                 exchange_ts=BASE_TS + 100, symbol="BTCUSDT"))
        transports["ETHUSDT"].connection("public").push(
            depth_update_message(first_update_id=100, last_update_id=200, previous_update_id=100,
                                 bids=((3000.0, 5.0),), speed="100ms",
                                 exchange_ts=BASE_TS + 100, symbol="ETHUSDT"))
        collector.pump_once()
        btc = collector.latest_state("BTCUSDT")
        eth = collector.latest_state("ETHUSDT")
        self.assertIsNotNone(btc)
        self.assertIsNotNone(eth)
        self.assertAlmostEqual(btc.price.best_bid, 60000.0)
        self.assertAlmostEqual(eth.price.best_bid, 3000.0)

    def test_one_symbol_failure_does_not_corrupt_others(self) -> None:
        collector, transports, _ = self._wired()
        # BTC 收到无法解析的报文（malformed）
        transports["BTCUSDT"].connection("public").push('{"stream":"btcusdt@depth@100ms","data":{}}')
        transports["ETHUSDT"].connection("public").push(
            depth_update_message(first_update_id=100, last_update_id=200, previous_update_id=100,
                                 bids=((3000.0, 5.0),), speed="100ms",
                                 exchange_ts=BASE_TS + 100, symbol="ETHUSDT"))
        collector.pump_once()
        # ETH 仍有有效状态；BTC 没有产出 state（畸形被计入其 telemetry）
        self.assertIsNotNone(collector.latest_state("ETHUSDT"))
        btc_telemetry = collector.runtime("BTCUSDT").telemetry
        self.assertGreaterEqual(btc_telemetry.malformed_message_count, 1)
        # 状态按标的独立
        eth_telemetry = collector.runtime("ETHUSDT").telemetry
        self.assertEqual(eth_telemetry.malformed_message_count, 0)

    def test_subscription_views_are_per_symbol(self) -> None:
        collector, transports, _ = self._wired()
        transports["ETHUSDT"].connection("public").push(
            depth_update_message(first_update_id=100, last_update_id=200, previous_update_id=100,
                                 bids=((3000.0, 5.0),), speed="100ms",
                                 exchange_ts=BASE_TS + 100, symbol="ETHUSDT"))
        collector.pump_once()
        views = {s.symbol: s for s in collector.subscriptions()}
        self.assertTrue(views["ETHUSDT"].subscribed)
        self.assertEqual(views["BTCUSDT"].symbol, "BTCUSDT")

    def test_max_symbols_enforced_and_duplicates_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CollectorConfig(symbols=tuple(f"S{i}USDT" for i in range(17)), max_symbols=16)
        with self.assertRaises(ValueError):
            CollectorConfig(symbols=("BTCUSDT", "BTCUSDT"), max_symbols=16)
        with self.assertRaises(ValueError):
            CollectorConfig(symbols=(), max_symbols=16)

    def test_on_batch_sink_receives_isolated_batches(self) -> None:
        import tempfile
        from pathlib import Path
        from storage.market_dataset import MarketDatasetWriter

        collector, transports, _ = self._wired()
        tmp = Path(tempfile.mkdtemp(prefix="mkt-sink-"))
        writers = {s: MarketDatasetWriter(tmp, venue="binance", symbol=s, kind="trade")
                   for s in collector.symbols}
        trades_seen = {s: 0 for s in collector.symbols}

        def sink(symbol, batch):
            for event in batch.market_events:
                from market.events.payloads import TradePayload
                if isinstance(event.payload, TradePayload):
                    writers[symbol].append(
                        exchange_ts=int(event.exchange_ts),
                        receive_ts=int(event.receive_ts),
                        payload={"price": float(event.payload.price),
                                 "qty": float(event.payload.quantity)})
                    trades_seen[symbol] += 1

        collector._on_batch = sink  # noqa: SLF001
        from tests.live_support import agg_trade_message
        transports["BTCUSDT"].connection("market").push(
            agg_trade_message(aggregate_trade_id=1, price=60000.0, quantity=0.5,
                              trade_ts=BASE_TS + 50, symbol="BTCUSDT"))
        for _ in range(6):  # runtime 在 public/market 两 tier 间轮转
            collector.pump_once()
        for w in writers.values():
            w.close()
        self.assertEqual(trades_seen["BTCUSDT"], 1)
        self.assertEqual(trades_seen["ETHUSDT"], 0)
        from storage.market_dataset import MarketDatasetReader
        btc = MarketDatasetReader(tmp, venue="binance", symbol="BTCUSDT", kind="trade").read()
        self.assertEqual(btc[0].payload["price"], 60000.0)


if __name__ == "__main__":
    unittest.main()
