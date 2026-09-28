"""P0001.9.1 故障测试：malformed / 快照失败 / 断线重连（SC-9 / SC-10）。

原则：单条坏数据或单次快照失败**不得**杀死 runtime；断线后必须重新建立可信状态
（`BookHealth != HEALTHY` 直到新的 snapshot + 增量对齐完成）。
"""

from __future__ import annotations

import json
import unittest

from market.health.state import BookHealth

from connectors.binance.market_data.endpoints import DEPTH_PATH, EXCHANGE_INFO_PATH, SERVER_TIME_PATH
from connectors.binance.market_data.errors import ReconnectExhaustedError, TransportError
from connectors.binance.market_data.runtime import LiveMarketDataRuntime
from tests.integration.test_live_market_data import build_runtime, pump_until, push_depth, push_market
from tests.live_support import (
    FakeHttp,
    ScriptedTransport,
    agg_trade_message,
    depth_snapshot_payload,
    depth_update_message,
    exchange_info_payload,
    live_config,
)
from tests.support import BASE_TS


def _pump_until_disconnect(runtime: LiveMarketDataRuntime, *, max_pumps: int = 6) -> None:
    """持续 pump 直到运行时记录了断开 + 重连（两条连接轮转，需要多轮）。"""
    for _ in range(max_pumps):
        runtime.pump_once(timeout_s=0.01)
        if runtime.telemetry.ws_disconnect_count >= 1 and runtime.telemetry.ws_reconnect_count >= 1:
            return
    raise AssertionError(f"disconnect not detected: {runtime.telemetry}")


class MalformedPayloadTest(unittest.TestCase):
    def test_sc10_bad_messages_are_counted_and_do_not_kill_the_runtime(self) -> None:
        runtime, transport, _ = build_runtime()
        push_depth(transport, "not json at all")
        push_depth(transport, json.dumps({"data": {"e": "unknownEvent"}}))
        push_depth(transport, depth_update_message(first_update_id=99, last_update_id=100, bids=((100.0, -1.0),)))
        push_depth(transport, depth_update_message(first_update_id=99, last_update_id=100))

        batches = pump_until(
            runtime,
            lambda batches: any(
                state.quality.book_health is BookHealth.HEALTHY for batch in batches for state in batch.states
            ),
        )

        errors = [error for batch in batches for error in batch.errors]
        self.assertEqual(len(errors), 3)
        self.assertEqual(runtime.telemetry.malformed_message_count, 3)
        self.assertIsNotNone(runtime.telemetry.last_error)
        self.assertEqual(runtime.telemetry.snapshot_count, 1)

    def test_message_for_an_unexpected_stream_is_rejected(self) -> None:
        runtime, transport, _ = build_runtime()
        push_depth(transport, agg_trade_message(aggregate_trade_id=1))

        batches = pump_until(runtime, lambda batches: any(batch.errors for batch in batches))

        errors = [error for batch in batches for error in batch.errors]
        self.assertIn("unexpected stream", errors[0])
        self.assertEqual(runtime.telemetry.malformed_message_count, 1)
        self.assertEqual(runtime.telemetry.agg_trade_count, 0)

    def test_symbol_mismatch_is_rejected(self) -> None:
        runtime, transport, _ = build_runtime()
        push_market(transport, agg_trade_message(aggregate_trade_id=1, symbol="ETHUSDT"))

        batches = pump_until(runtime, lambda batches: any(batch.errors for batch in batches))

        self.assertEqual(runtime.telemetry.agg_trade_count, 0)
        self.assertEqual(runtime.telemetry.malformed_message_count, 1)
        self.assertTrue(batches)


class SnapshotFailureTest(unittest.TestCase):
    def test_snapshot_failure_keeps_the_book_untrusted_and_retries(self) -> None:
        runtime, transport, http = build_runtime()
        http.failures[DEPTH_PATH] = 1
        push_depth(transport, depth_update_message(first_update_id=99, last_update_id=100))

        first = pump_until(runtime, lambda batches: any(batch.errors for batch in batches))
        self.assertEqual(runtime.telemetry.snapshot_failure_count, 1)
        self.assertEqual(runtime.telemetry.snapshot_count, 0)
        self.assertIs(runtime.engine.book_health, BookHealth.AWAITING_SNAPSHOT)

        push_depth(transport, depth_update_message(first_update_id=100, last_update_id=100))
        recovery = pump_until(
            runtime,
            lambda batches: any(
                state.quality.book_health is BookHealth.HEALTHY for batch in batches for state in batch.states
            ),
        )

        self.assertTrue(first)
        self.assertEqual(runtime.telemetry.snapshot_count, 1)
        self.assertTrue(recovery)


class ReconnectTest(unittest.TestCase):
    def _connected(self):
        runtime, transport, http = build_runtime()
        push_depth(transport, depth_update_message(first_update_id=99, last_update_id=100))
        pump_until(
            runtime,
            lambda batches: any(
                state.quality.book_health is BookHealth.HEALTHY for batch in batches for state in batch.states
            ),
        )
        return runtime, transport, http

    def test_sc9_disconnect_invalidates_the_book_until_resync(self) -> None:
        runtime, transport, http = self._connected()
        http.responses[DEPTH_PATH] = depth_snapshot_payload(last_update_id=150, bids=((100.0, 4.0),))

        transport.connection("public").drop()
        _pump_until_disconnect(runtime)

        self.assertEqual(runtime.telemetry.ws_disconnect_count, 1)
        self.assertEqual(runtime.telemetry.ws_reconnect_count, 1)
        self.assertIs(runtime.engine.book_health, BookHealth.RESYNCING)  # 已失效，等待新快照
        self.assertNotEqual(runtime.engine.book_health, BookHealth.HEALTHY)

        push_depth(transport, depth_update_message(first_update_id=150, last_update_id=150))
        recovery = pump_until(
            runtime,
            lambda batches: any(
                state.quality.book_health is BookHealth.HEALTHY for batch in batches for state in batch.states
            ),
        )

        self.assertEqual(runtime.telemetry.snapshot_count, 2)
        self.assertTrue(recovery)

    def test_reconnect_is_retried_until_it_succeeds(self) -> None:
        runtime, transport, _ = self._connected()
        transport.connection("public").drop()
        transport.failures.append("boom")

        _pump_until_disconnect(runtime)

        self.assertEqual(runtime.telemetry.ws_disconnect_count, 1)
        self.assertEqual(runtime.telemetry.ws_reconnect_count, 1)  # 第二次尝试成功
        self.assertIn("boom", runtime.telemetry.last_error or "")

    def test_reconnect_exhaustion_is_raised_not_swallowed(self) -> None:
        runtime, transport, _ = self._connected()
        transport.connection("public").drop()
        for _ in range(live_config().reconnect.max_attempts):
            transport.failures.append("still down")

        with self.assertRaises(ReconnectExhaustedError):
            for _ in range(6):
                runtime.pump_once(timeout_s=0.01)

        self.assertGreaterEqual(runtime.telemetry.ws_disconnect_count, 1)

    def test_silence_is_a_timeout_not_a_disconnect(self) -> None:
        runtime, transport, _ = self._connected()

        batch = runtime.pump_once(timeout_s=0.01)  # 轮到 market：静默
        if not batch.timed_out:
            batch = runtime.pump_once(timeout_s=0.01)

        self.assertTrue(batch.timed_out)
        self.assertEqual(runtime.telemetry.ws_timeout_count, 1)
        self.assertEqual(runtime.telemetry.ws_disconnect_count, 0)

    def test_pump_before_connect_is_an_error(self) -> None:
        transport = ScriptedTransport()
        runtime = LiveMarketDataRuntime(
            config=live_config(),
            http_client=FakeHttp(
                responses={
                    DEPTH_PATH: depth_snapshot_payload(last_update_id=1),
                    EXCHANGE_INFO_PATH: exchange_info_payload(),
                    SERVER_TIME_PATH: {"serverTime": BASE_TS},
                }
            ),
            transport_factory=transport,
            clock=lambda: BASE_TS,
            sleeper=lambda _seconds: None,
        )

        with self.assertRaises(TransportError):
            runtime.pump_once(timeout_s=0.01)


if __name__ == "__main__":
    unittest.main()
