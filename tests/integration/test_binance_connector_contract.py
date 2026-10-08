"""P0001.15 验收 C：Binance connector adapter 必须分别暴露 market / private health 与 facts。

离线验证（不发起真实交易、不联网）：既有 Binance 实现以受控 double 注入 —— adapter 只做**映射**，
不重写 REST/WS/user stream/TradingRules/rate limit/reconciliation/UNKNOWN 语义。
"""

from __future__ import annotations

import unittest

from connectors.binance.execution_connector import BinancePrivateExecutionConnector
from connectors.binance.market_connector import BinanceMarketDataConnector
from domain.instruments import PriceType
from execution.events import OrderAccepted
from market.events.types import Milliseconds
from venue import VenueEnvironment, binance_venue, classify_submission
from venue.health import ConnectionState

BINANCE = binance_venue(environment=VenueEnvironment.TESTNET)


class _Telemetry:
    def __init__(self, **overrides: object) -> None:
        self.ws_connect_count = 1
        self.ws_reconnect_count = 0
        self.ws_disconnect_count = 0
        self.ws_timeout_count = 0
        self.message_count = 10
        self.ack_count = 1
        self.malformed_message_count = 0
        self.depth_event_count = 5
        self.depth_gap_count = 0
        self.state_count = 5
        self.resync_count = 0
        self.snapshot_count = 1
        self.snapshot_failure_count = 0
        self.resync_suppressed_count = 0
        self.snapshot_latency_ms = 12
        self.agg_trade_count = 4
        self.duplicate_trade_count = 0
        self.mark_update_count = 3
        self.event_lag_ms = 40
        self.mark_age_ms = 25
        self.server_time_offset_ms = -3
        self.market_generation = 1
        self.last_error = None
        for key, value in overrides.items():
            setattr(self, key, value)


class _Mark:
    def __init__(self) -> None:
        self.price = 61_000.0
        self.exchange_ts = 1_000
        self.receive_ts = 1_010


class _State:
    class _Price:
        best_bid = 60_999.5
        best_ask = 61_000.5

    class _Trade:
        last_price = 61_000.0

    class _Quality:
        book_health = "healthy"
        tradeable = True

    price = _Price()
    trade = _Trade()
    quality = _Quality()


class _LiveMarketDataRuntime:
    """`LiveMarketDataRuntime` 的受控替身（只暴露 adapter 真正读取的事实）。"""

    def __init__(self) -> None:
        self.connected = False
        self.closed = False
        self._mark = _Mark()
        self._telemetry = _Telemetry()

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.closed = True

    def trading_rules(self) -> object:
        return "TradingRules"

    def latest_mark(self) -> object:
        return self._mark

    def history(self) -> tuple[object, ...]:
        return (_State(),)

    def telemetry(self) -> object:
        return self._telemetry


class _PrivateTelemetry:
    def __init__(self) -> None:
        self.last_receive_lag_ms = 55
        self.server_time_offset_ms = -3
        self.message_count = 7


class _PrivateAccountRuntime:
    def __init__(self) -> None:
        self.lifecycle = "STOPPED"
        self.started = False
        self.stopped = False
        self._telemetry = _PrivateTelemetry()

    def start(self) -> object:
        self.started = True
        self.lifecycle = "ACTIVE"
        return object()

    def stop(self) -> None:
        self.stopped = True
        self.lifecycle = "STOPPED"

    def lifecycle_state(self) -> str:
        return self.lifecycle

    def latest_snapshot(self) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(process_ts=9_000, can_trade=True)

    def latest_position(self) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(symbol="BTCUSDT", quantity=0.0)

    def telemetry(self) -> object:
        return self._telemetry


class _ExecutionAdapter:
    def __init__(self) -> None:
        self.submitted: list[object] = []

    def submit(self, order: object) -> tuple[object, ...]:
        self.submitted.append(order)
        return (OrderAccepted(client_order_id="c1", exchange_order_id="x1", timestamp=5),)

    def cancel(self, order: object) -> tuple[object, ...]:
        return ()

    def poll(self) -> tuple[object, ...]:
        return ()

    def open_orders(self) -> tuple[object, ...]:
        return ()

    def recent_fills(self, *, since_ms: Milliseconds | None = None) -> tuple[object, ...]:
        return ()


class BinanceMarketConnectorTest(unittest.TestCase):
    def connector(self, runtime: _LiveMarketDataRuntime | None = None) -> BinanceMarketDataConnector:
        return BinanceMarketDataConnector(runtime=runtime or _LiveMarketDataRuntime(),
                                          venue_identity=BINANCE, instrument_id="binance:BTCUSDT",
                                          clock=lambda: 10_000)

    def test_market_connector_exposes_rules_facts_timestamps_and_health(self) -> None:
        runtime = _LiveMarketDataRuntime()
        connector = self.connector(runtime)
        connector.connect()

        self.assertTrue(runtime.connected)
        self.assertEqual(connector.connector_id, "binance:market")
        self.assertEqual(connector.trading_rules("BTCUSDT"), "TradingRules")

        facts = connector.latest_facts(now_ms=10_000)
        self.assertEqual(facts.best_bid, 60_999.5)
        self.assertEqual(facts.best_ask, 61_000.5)
        self.assertEqual(facts.last_trade_price, 61_000.0)
        self.assertEqual(facts.book_health, "healthy")
        self.assertIs(facts.tradeable, True)

        stamps = connector.market_timestamps(now_ms=10_000)
        self.assertTrue(stamps.observed)
        self.assertEqual(stamps.event_age_ms, 40)
        self.assertEqual(stamps.clock_offset_ms, -3)

        health = connector.health(now_ms=10_000)
        self.assertEqual(health.connection_state, ConnectionState.CONNECTED)
        self.assertEqual(health.event_age_ms, 40)
        self.assertTrue(health.events_observed)
        self.assertEqual(health.reconnect_count, 0)

    def test_market_connector_reports_reconnect_and_failure_honestly(self) -> None:
        runtime = _LiveMarketDataRuntime()
        connector = self.connector(runtime)
        connector.connect()
        runtime._telemetry.ws_reconnect_count = 1

        self.assertEqual(connector.health(now_ms=10_000).connection_state, ConnectionState.RECONNECTING)
        self.assertEqual(connector.health(now_ms=10_000).connection_state, ConnectionState.CONNECTED)
        connector.disconnect()
        self.assertEqual(connector.health(now_ms=10_000).connection_state, ConnectionState.DISCONNECTED)

    def test_market_connector_mark_source_is_mark_only(self) -> None:
        runtime = _LiveMarketDataRuntime()
        connector = self.connector(runtime)

        source = connector.reference_price_source()
        reference = source.latest(now_ms=10_000)
        self.assertTrue(reference.known)                       # 来自正式 markPriceUpdate 观测
        self.assertEqual(reference.price_type, PriceType.MARK)
        self.assertEqual(reference.price, 61_000.0)
        self.assertIn("mark_price_update", reference.source)
        self.assertEqual(source.price_type, PriceType.MARK)
        # 不是 last trade（last trade 也是 61000.0，但来源必须明确是 mark）
        runtime._mark.price = 61_250.0
        self.assertEqual(source.latest(now_ms=10_100).price, 61_250.0)

    def test_market_connector_without_mark_reports_unknown(self) -> None:
        runtime = _LiveMarketDataRuntime()
        runtime.latest_mark = lambda: None                     # type: ignore[method-assign]
        connector = self.connector(runtime)

        reference = connector.reference_price_source().latest(now_ms=10_000)
        self.assertFalse(reference.known)
        self.assertIsNone(reference.price)
        self.assertEqual(reference.reason, "REFERENCE_PRICE_MARK_UNAVAILABLE")


class BinancePrivateConnectorTest(unittest.TestCase):
    def connector(self, *, runtime: object | None = ..., adapter: object | None = None
                  ) -> BinancePrivateExecutionConnector:
        return BinancePrivateExecutionConnector(
            adapter=adapter or _ExecutionAdapter(), venue_identity=BINANCE, symbol="BTCUSDT",
            clock=lambda: 10_000,
            runtime=_PrivateAccountRuntime() if runtime is ... else runtime,
            reconciliation_state_provider=lambda: "RECONCILED",
            rate_limit_provider=lambda: type("R", (), {"status": "HEALTHY"})())

    def test_private_connector_exposes_account_positions_and_health(self) -> None:
        runtime = _PrivateAccountRuntime()
        connector = self.connector(runtime=runtime)
        connector.connect()

        self.assertTrue(runtime.started)
        self.assertEqual(connector.connector_id, "binance:execution")
        snapshot = connector.account_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.can_trade, True)
        positions = connector.positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(connector.reconciliation_state(), "RECONCILED")

        health = connector.health(now_ms=10_000)
        self.assertEqual(health.connection_state, ConnectionState.CONNECTED)
        self.assertEqual(health.reconciliation_state, "RECONCILED")
        self.assertEqual(health.rate_limit_state, "HEALTHY")
        self.assertEqual(health.account_state_freshness_ms, 1_000)
        self.assertTrue(health.private_events_observed)

    def test_private_connector_maps_listen_key_lifecycle_states(self) -> None:
        runtime = _PrivateAccountRuntime()
        connector = self.connector(runtime=runtime)
        connector.connect()

        for raw, expected in (("STOPPED", ConnectionState.DISCONNECTED),
                              ("STARTING", ConnectionState.CONNECTING),
                              ("RENEWING", ConnectionState.CONNECTED),
                              ("RECONNECTING", ConnectionState.RECONNECTING),
                              ("EXPIRED", ConnectionState.FAILED),
                              ("FAILED", ConnectionState.FAILED),
                              ("???", ConnectionState.UNKNOWN)):
            with self.subTest(raw=raw):
                runtime.lifecycle = raw
                self.assertEqual(connector.health(now_ms=10_000).connection_state, expected)

    def test_private_connector_keeps_unknown_when_runtime_is_absent(self) -> None:
        connector = BinancePrivateExecutionConnector(
            adapter=_ExecutionAdapter(), venue_identity=BINANCE, symbol="BTCUSDT", clock=lambda: 10_000)
        connector.connect()

        health = connector.health(now_ms=10_000)
        self.assertEqual(health.connection_state, ConnectionState.DISCONNECTED)   # 未连接（不是"无事件"）
        self.assertIsNone(health.last_private_event_ms)
        self.assertFalse(health.private_events_observed)
        self.assertIsNone(connector.account_snapshot())          # 未注入 runtime ⇒ UNKNOWN，不伪造账户
        self.assertEqual(connector.positions(), ())

    def test_three_way_submission_semantics_are_preserved(self) -> None:
        connector = self.connector()
        order = type("O", (), {"client_order_id": "c1"})()

        events = connector.submit(order)
        self.assertEqual(classify_submission(events).value, "ACCEPTED")

    def test_health_without_rate_limit_provider_is_unknown_not_healthy(self) -> None:
        connector = BinancePrivateExecutionConnector(
            adapter=_ExecutionAdapter(), venue_identity=BINANCE, symbol="BTCUSDT", clock=lambda: 10_000)
        connector.connect()

        health = connector.health(now_ms=10_000)
        self.assertIsNone(health.rate_limit_state)
        self.assertIsNone(health.reconciliation_state)          # 未注入 ⇒ UNKNOWN，不伪造 RECONCILED


if __name__ == "__main__":
    unittest.main()
