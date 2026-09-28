"""P0001.9.2 故障测试：keepalive / 过期 / 重连 / 去重 / 快照失败（SC-9 – SC-13）。"""

from __future__ import annotations

import unittest
from collections import deque

from connectors.binance.market_data.errors import TransportError, WebSocketTimeout
from connectors.binance.private.errors import ListenKeyError, PrivateResponseError, ReconnectExhaustedError
from tests.private_support import (
    SYMBOL,
    account_update_message,
    build_runtime,
    listen_key_expired_message,
    order_update_message,
)
from tests.support import BASE_TS

KEEPALIVE_INTERVAL = 1_000
TTL = 4_000


class Clock:
    """可变测试时钟（毫秒）。"""

    def __init__(self, now: int = BASE_TS) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, delta: int) -> None:
        self.now += delta


def _runtime(**overrides):
    clock = Clock()
    runtime, fetcher, factory = build_runtime(
        clock=clock,
        keepalive_interval_ms=KEEPALIVE_INTERVAL,
        listen_key_ttl_ms=TTL,
        **overrides,
    )
    runtime.start()
    return runtime, fetcher, factory, clock


class KeepaliveTest(unittest.TestCase):
    def test_sc9_keepalive_is_sent_when_due(self) -> None:
        runtime, fetcher, _, clock = _runtime()

        clock.advance(KEEPALIVE_INTERVAL)
        runtime.pump_once(timeout_s=0.01)

        self.assertIn(("PUT", "/fapi/v1/listenKey"), fetcher.calls)
        telemetry = runtime.telemetry
        self.assertEqual(telemetry.keepalive_count, 1)
        self.assertEqual(telemetry.listen_key_state, "ACTIVE")

    def test_keepalive_failure_fails_the_lifecycle(self) -> None:
        runtime, fetcher, _, clock = _runtime()
        fetcher.failures["/fapi/v1/listenKey"] = 1
        clock.advance(KEEPALIVE_INTERVAL)

        with self.assertRaises(ListenKeyError):
            runtime.pump_once(timeout_s=0.01)

        telemetry = runtime.telemetry
        self.assertEqual(telemetry.keepalive_failure_count, 1)
        self.assertEqual(telemetry.listen_key_state, "FAILED")
        self.assertIn("keepalive failed", telemetry.last_error or "")

    def test_sc9_ttl_expiry_recreates_the_listen_key(self) -> None:
        runtime, fetcher, factory, clock = _runtime()
        fetcher.listen_keys = deque([fetcher.listen_keys[0], "LK-SECOND", "LK-THIRD"])

        clock.advance(TTL)
        runtime.pump_once(timeout_s=0.01)

        telemetry = runtime.telemetry
        self.assertEqual(telemetry.listen_key_expired_count, 1)
        self.assertEqual(telemetry.listen_key_created_count, 2)
        self.assertFalse(runtime.continuity_assumed)
        self.assertIn("LK-SECOND", factory.urls[-1])

    def test_sc9_listen_key_expired_event_recreates_the_listen_key(self) -> None:
        runtime, fetcher, factory, _ = _runtime()
        fetcher.listen_keys = deque([fetcher.listen_keys[0], "LK-SECOND", "LK-THIRD"])
        factory.connection.push(listen_key_expired_message())

        batch = runtime.pump_once(timeout_s=0.01)

        self.assertTrue(any("listenKey" in error for error in batch.errors))
        self.assertEqual(runtime.telemetry.listen_key_expired_count, 1)
        self.assertEqual(runtime.lifecycle_state, "ACTIVE")
        self.assertFalse(runtime.continuity_assumed)
        self.assertIn("LK-SECOND", factory.urls[-1])


class ReconnectTest(unittest.TestCase):
    def test_sc10_socket_drop_reconnects_but_drops_continuity(self) -> None:
        runtime, fetcher, factory, _ = _runtime()
        factory.connection.drop()

        runtime.pump_once(timeout_s=0.01)

        telemetry = runtime.telemetry
        self.assertEqual(telemetry.disconnect_count, 1)
        self.assertEqual(telemetry.reconnect_count, 1)
        self.assertFalse(runtime.continuity_assumed)  # 不静默假设状态连续
        self.assertEqual(runtime.lifecycle_state, "ACTIVE")
        self.assertIn(runtime.lifecycle.listen_key or "", factory.urls[-1])

    def test_snapshot_restores_continuity_after_reconnect(self) -> None:
        runtime, _, factory, _ = _runtime()
        factory.connection.drop()
        runtime.pump_once(timeout_s=0.01)
        self.assertFalse(runtime.continuity_assumed)

        boundary = runtime.refresh_snapshot()

        self.assertEqual(boundary.symbol, SYMBOL)
        self.assertTrue(runtime.continuity_assumed)
        self.assertEqual(runtime.telemetry.snapshot_count, 2)

    def test_reconnect_retries_then_exhausts(self) -> None:
        runtime, _, factory, _ = _runtime()
        factory.connection.drop()
        for _ in range(3):
            factory.failures.append("still down")

        with self.assertRaises(ReconnectExhaustedError):
            for _ in range(3):
                runtime.pump_once(timeout_s=0.01)

        self.assertGreaterEqual(runtime.telemetry.disconnect_count, 1)

    def test_silence_is_a_timeout_not_a_disconnect(self) -> None:
        runtime, _, _, _ = _runtime()

        batch = runtime.pump_once(timeout_s=0.01)

        self.assertTrue(batch.timed_out)
        self.assertEqual(runtime.telemetry.timeout_count, 1)
        self.assertEqual(runtime.telemetry.disconnect_count, 0)
        self.assertTrue(runtime.continuity_assumed)


class EventOrderingFaultTest(unittest.TestCase):
    def test_sc11_out_of_order_order_update_is_rejected(self) -> None:
        runtime, _, factory, _ = _runtime()
        factory.connection.push(order_update_message(cumulative_fill_quantity="0.5", trade_id=5, client_order_id="cid-a"))
        factory.connection.push(order_update_message(cumulative_fill_quantity="0.2", trade_id=6, client_order_id="cid-a"))

        first = runtime.pump_once(timeout_s=0.01)
        second = runtime.pump_once(timeout_s=0.01)

        self.assertEqual(len(first.events), 1)
        self.assertEqual(second.events, ())
        self.assertEqual(runtime.telemetry.out_of_order_count, 1)
        self.assertEqual(runtime.telemetry.order_update_count, 1)

    def test_duplicate_account_update_is_rejected(self) -> None:
        runtime, _, factory, _ = _runtime()
        factory.connection.push(account_update_message())
        factory.connection.push(account_update_message())

        runtime.pump_once(timeout_s=0.01)
        runtime.pump_once(timeout_s=0.01)

        self.assertEqual(runtime.telemetry.account_update_count, 1)
        self.assertEqual(runtime.telemetry.duplicate_count, 1)


class SnapshotFailureTest(unittest.TestCase):
    def test_snapshot_failure_is_counted_and_reraised(self) -> None:
        runtime, fetcher, _, _ = _runtime()
        fetcher.failures["/fapi/v2/account"] = 1

        with self.assertRaises(TransportError):
            runtime.refresh_snapshot()

        telemetry = runtime.telemetry
        self.assertEqual(telemetry.snapshot_failure_count, 1)
        self.assertFalse(runtime.continuity_assumed)
        self.assertIn("snapshot failed", telemetry.last_error or "")

    def test_hedge_mode_snapshot_fails_closed(self) -> None:
        from connectors.binance.private.errors import UnsupportedAccountModeError
        from tests.private_support import position_risk_payload

        runtime, fetcher, _, _ = _runtime()
        fetcher.responses["/fapi/v2/positionRisk"] = position_risk_payload(position_side="SHORT")

        with self.assertRaises(UnsupportedAccountModeError):
            runtime.refresh_snapshot()


class LatencyTest(unittest.TestCase):
    def test_sc12_latency_distribution_is_measured(self) -> None:
        runtime, _, factory, _ = _runtime()
        for index, lag in enumerate((10, 20, 30, 40, 5_000)):
            factory.connection.push(
                order_update_message(
                    event_ts=BASE_TS - lag, trade_id=100 + index, cumulative_fill_quantity=f"0.{index + 1}"
                )
            )

        for _ in range(6):
            runtime.pump_once(timeout_s=0.01)

        distribution = runtime.telemetry.private_lag_ms
        self.assertIsNotNone(distribution)
        self.assertEqual(distribution.samples, 5)  # type: ignore[union-attr]
        self.assertEqual(distribution.minimum, 10)  # type: ignore[union-attr]
        self.assertEqual(distribution.median, 30)  # type: ignore[union-attr]
        self.assertEqual(distribution.maximum, 5_000)  # type: ignore[union-attr]
        # SC-13 的门控按 **median** 判定（单点尖峰不改变 median）
        self.assertTrue(runtime.lag_within_threshold())

    def test_sc13_sustained_lag_breaches_the_threshold(self) -> None:
        runtime, _, factory, _ = _runtime(max_median_private_lag_ms=1_000)
        for index in range(3):
            factory.connection.push(
                order_update_message(
                    event_ts=BASE_TS - 3_000, trade_id=200 + index, cumulative_fill_quantity=f"0.{index + 1}"
                )
            )

        for _ in range(4):
            runtime.pump_once(timeout_s=0.01)

        distribution = runtime.telemetry.private_lag_ms
        self.assertEqual(distribution.median, 3_000)  # type: ignore[union-attr]
        self.assertFalse(runtime.lag_within_threshold())

    def test_websocket_timeout_does_not_raise(self) -> None:
        runtime, _, _, _ = _runtime()

        for _ in range(3):
            runtime.pump_once(timeout_s=0.01)

        self.assertEqual(runtime.telemetry.timeout_count, 3)
        self.assertIsInstance(WebSocketTimeout("x"), TransportError)


if __name__ == "__main__":
    unittest.main()
