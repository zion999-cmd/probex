"""P0001.9.2 集成测试：私有运行时启动顺序、事件消费与 telemetry（SC-1 / SC-4 / SC-6 – SC-9 / SC-12）。"""

from __future__ import annotations

import unittest

from connectors.binance.private.errors import CredentialsError, PrivateStreamError
from connectors.binance.private.events import UserEventType
from connectors.binance.private.runtime import PrivateAccountRuntime
from connectors.binance.private.rest import PrivateRestClient
from connectors.binance.private.user_stream import UserStreamClient
from tests.private_support import (
    FAKE_LISTEN_KEY,
    SYMBOL,
    ScriptedStreamFactory,
    account_update_message,
    build_runtime,
    credentials,
    listen_key_expired_message,
    order_update_message,
    private_config,
)
from tests.support import BASE_TS


class StartupTest(unittest.TestCase):
    def test_sc1_runtime_refuses_to_start_without_credentials(self) -> None:
        runtime, _, _ = build_runtime()
        runtime.credentials = None

        with self.assertRaises(CredentialsError) as ctx:
            runtime.start()

        self.assertIn("refusing to start", str(ctx.exception))
        self.assertFalse(runtime.continuity_assumed)

    def test_start_order_is_stream_then_snapshot(self) -> None:
        runtime, fetcher, factory = build_runtime()

        boundary = runtime.start()

        # listenKey → WS → account → position
        self.assertEqual(
            [call for call in fetcher.calls],
            [("GET", "/fapi/v1/time"), ("POST", "/fapi/v1/listenKey"), ("GET", "/fapi/v2/account"), ("GET", "/fapi/v2/positionRisk")],
        )
        self.assertEqual(len(factory.urls), 1)
        self.assertIn(FAKE_LISTEN_KEY, factory.urls[0])
        self.assertEqual(boundary.symbol, SYMBOL)
        self.assertEqual(boundary.account_received_ts, BASE_TS)
        self.assertEqual(boundary.stream_connected_at_ms, BASE_TS)
        self.assertTrue(runtime.continuity_assumed)
        self.assertEqual(runtime.lifecycle_state, "ACTIVE")
        self.assertEqual(runtime.telemetry.listen_key_created_count, 1)
        self.assertEqual(runtime.telemetry.snapshot_count, 1)

    def test_sc4_snapshot_facts_are_exposed(self) -> None:
        runtime, _, _ = build_runtime()

        runtime.start()

        self.assertIsNotNone(runtime.latest_snapshot)
        self.assertAlmostEqual(runtime.latest_snapshot.settlement_balance.balance, 1000.50)  # type: ignore[union-attr]
        self.assertIsNotNone(runtime.latest_position)
        position = runtime.latest_position
        self.assertAlmostEqual(position.position_amt, 0.5)  # type: ignore[union-attr]
        self.assertAlmostEqual(position.liquidation_price, 55000.0)  # type: ignore[union-attr]
        self.assertAlmostEqual(position.mark_price, 60100.0)  # type: ignore[union-attr]

    def test_sc5_stream_connected_at_ms_is_the_ws_connect_instant(self) -> None:
        """boundary 的 stream_connected_at_ms 必须是真实 WS 连接时刻，而不是快照开始时刻。"""
        from tests.private_support import build_runtime
        from tests.fault.test_private_stream_faults import Clock

        clock = Clock()
        runtime, _, factory = build_runtime(clock=clock)
        runtime.start()
        clock.advance(5_000)  # WS 已连接，稍后才刷新快照

        boundary = runtime.refresh_snapshot()

        self.assertEqual(boundary.stream_connected_at_ms, BASE_TS)  # connect 时刻
        self.assertEqual(boundary.snapshot_started_at_ms, BASE_TS + 5_000)  # 快照开始时刻
        self.assertGreater(boundary.snapshot_started_at_ms, boundary.stream_connected_at_ms)
        self.assertEqual(boundary.account_received_ts, BASE_TS + 5_000)

    def test_snapshot_requires_a_connected_stream(self) -> None:
        runtime, _, _ = build_runtime()

        with self.assertRaises(PrivateStreamError):
            runtime.refresh_snapshot()

    def test_account_without_usdt_asset_fails_closed(self) -> None:
        """payload 未提供 marginAsset 时，USDT-M 由账户资产列表核对；没有 USDT 条目 ⇒ fail closed。"""
        from connectors.binance.private.errors import UnsupportedAccountModeError
        from tests.private_support import account_payload

        runtime, fetcher, _ = build_runtime()
        payload = account_payload()
        payload["assets"] = [
            {
                "asset": "FDUSD",
                "walletBalance": "10.0",
                "availableBalance": "10.0",
                "marginBalance": "10.0",
                "unrealizedProfit": "0.0",
                "maxWithdrawAmount": "10.0",
                "updateTime": BASE_TS,
            }
        ]
        fetcher.responses["/fapi/v2/account"] = payload

        with self.assertRaises(UnsupportedAccountModeError):
            runtime.start()

    def test_server_time_offset_is_recorded(self) -> None:
        runtime, _, _ = build_runtime()

        runtime.start()

        self.assertEqual(runtime.telemetry.server_time_offset_ms, 500)

    def test_pump_before_start_is_an_error(self) -> None:
        runtime, _, _ = build_runtime()

        with self.assertRaises(PrivateStreamError):
            runtime.pump_once(timeout_s=0.01)


class EventConsumerTest(unittest.TestCase):
    def _started(self):
        runtime, fetcher, factory = build_runtime()
        runtime.start()
        return runtime, fetcher, factory

    def test_sc7_account_update_is_consumed_and_measured(self) -> None:
        runtime, _, factory = self._started()
        factory.connection.push(account_update_message(event_ts=BASE_TS - 40))

        batch = runtime.pump_once(timeout_s=0.01)

        self.assertEqual([event.event_type for event in batch.events], [UserEventType.ACCOUNT_UPDATE])
        telemetry = runtime.telemetry
        self.assertEqual(telemetry.account_update_count, 1)
        self.assertEqual(telemetry.last_receive_lag_ms, 40)
        self.assertEqual(telemetry.private_lag_ms.samples, 1)  # type: ignore[union-attr]
        self.assertTrue(runtime.lag_within_threshold())

    def test_sc8_order_trade_update_is_consumed(self) -> None:
        runtime, _, factory = self._started()
        factory.connection.push(order_update_message(event_ts=BASE_TS - 10))

        batch = runtime.pump_once(timeout_s=0.01)

        event = batch.events[0]
        self.assertIs(event.event_type, UserEventType.ORDER_TRADE_UPDATE)
        self.assertEqual(event.observation.client_order_id, "probex-s1-000001")  # type: ignore[union-attr]
        telemetry = runtime.telemetry
        self.assertEqual((telemetry.order_update_count, telemetry.fill_observation_count), (1, 1))
        self.assertTrue(runtime.continuity_assumed)  # 消费事实不改变「已建立快照边界」这一前提

    def test_unsupported_and_malformed_messages_are_counted(self) -> None:
        runtime, _, factory = self._started()
        factory.connection.push('{"e":"MARGIN_CALL","E":1}')
        factory.connection.push("not json")
        factory.connection.push(account_update_message())

        batches = [runtime.pump_once(timeout_s=0.01) for _ in range(4)]

        telemetry = runtime.telemetry
        self.assertEqual(telemetry.unsupported_event_count, 1)
        self.assertEqual(telemetry.malformed_count, 1)
        self.assertEqual(telemetry.account_update_count, 1)
        self.assertTrue(any(batch.errors for batch in batches))

    def test_duplicate_events_are_not_consumed_twice(self) -> None:
        runtime, _, factory = self._started()
        factory.connection.push(order_update_message())
        factory.connection.push(order_update_message())

        runtime.pump_once(timeout_s=0.01)
        second = runtime.pump_once(timeout_s=0.01)

        self.assertEqual(second.events, ())
        self.assertEqual(runtime.telemetry.duplicate_count, 1)
        self.assertEqual(runtime.telemetry.order_update_count, 1)

    def test_multi_message_drain(self) -> None:
        runtime, _, factory = self._started()
        for index in range(3):
            factory.connection.push(order_update_message(trade_id=100 + index, cumulative_fill_quantity=f"0.{index + 1}"))

        batch = runtime.pump_once(timeout_s=0.01, max_messages=8)

        self.assertEqual(len(batch.events), 3)
        self.assertEqual(runtime.telemetry.order_update_count, 3)

    def test_heartbeat_count_is_reported_from_the_transport(self) -> None:
        runtime, _, factory = self._started()
        factory.connection.ping_count = 3

        runtime.pump_once(timeout_s=0.01)

        self.assertEqual(runtime.telemetry.heartbeat_count, 3)

    def test_stop_closes_stream_and_listen_key(self) -> None:
        runtime, fetcher, factory = self._started()

        runtime.stop()

        self.assertEqual(runtime.lifecycle_state, "STOPPED")
        self.assertEqual(fetcher.calls[-1], ("DELETE", "/fapi/v1/listenKey"))
        self.assertTrue(factory.connection.closed)


class LatencyThresholdTest(unittest.TestCase):
    def test_sc13_lag_threshold_judgement(self) -> None:
        runtime, _, factory = build_runtime(max_median_private_lag_ms=100)
        runtime.start()
        factory.connection.push(account_update_message(event_ts=BASE_TS - 500))

        runtime.pump_once(timeout_s=0.01)

        self.assertFalse(runtime.lag_within_threshold())

    def test_no_samples_means_threshold_not_met(self) -> None:
        runtime, _, _ = build_runtime()
        runtime.start()

        self.assertFalse(runtime.lag_within_threshold())


if __name__ == "__main__":
    unittest.main()
