"""P0001.9.3.1 SC-4 / SC-5：runtime 真实断线路径必须**自动**让 recovery 失效。

契约：`StartupRecovery` 订阅 `PrivateAccountRuntime.subscribe_discontinuity(listener)`；
运行时真的断线/重建 listenKey/停止时，recovery 状态立刻回落 `NOT_RECOVERED`——
测试不得通过手工调用 `recovery.invalidate()` 来模拟"已经接线"。
"""

from __future__ import annotations

import unittest

from connectors.binance.private.recovery import RecoveryStatus
from tests.private_support import (
    SYMBOL,
    FakeConnection,
    account_payload,
    build_recovery,
    build_runtime,
    listen_key_expired_message,
    order_update_message,
    position_risk_payload,
    recovery_responses,
    stream_state,
)
from tests.support import BASE_TS


def _flat() -> list[dict]:
    return position_risk_payload(position_amt="0", entry_price="0", mark_price="0")


class RuntimeRecoveryWiringTest(unittest.TestCase):
    def _wired(self):
        """真实运行时 + recovery（已订阅断线事件），并已完成一次成功的恢复。"""
        runtime, fetcher, factory = build_runtime()
        recovery, _, _, accounting = build_recovery(
            responses=recovery_responses(position=_flat()), clock=lambda: BASE_TS
        )
        recovery.bind(runtime)  # SC-4：真实接线（不是手工 invalidate）
        runtime.start()
        result = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)
        self.assertIs(result.status, RecoveryStatus.RECOVERED)
        return runtime, fetcher, factory, recovery, accounting

    def test_sc4_bind_requires_a_subscribable_runtime(self) -> None:
        from connectors.binance.private.errors import PrivateFormatError

        recovery, _, _, _ = build_recovery(responses=recovery_responses(position=_flat()))
        with self.assertRaises(PrivateFormatError):
            recovery.bind(object())

    def test_sc4_real_disconnect_auto_invalidates_without_manual_call(self) -> None:
        runtime, _fetcher, factory, recovery, _accounting = self._wired()
        self.assertIs(recovery.state, RecoveryStatus.RECOVERED)

        factory.connection.drop()  # 真实断线（对端关闭）→ 由 pump_once 走重连路径
        runtime.pump_once(timeout_s=0.01)

        self.assertIs(recovery.state, RecoveryStatus.NOT_RECOVERED)  # 无需手工 invalidate
        self.assertFalse(runtime.continuity_assumed)
        self.assertTrue(any("disconnected" in event for event in runtime.discontinuity_events))
        self.assertTrue(recovery.reason_log)

    def test_sc4_listen_key_recreation_auto_invalidates(self) -> None:
        runtime, _fetcher, factory, recovery, _accounting = self._wired()

        factory.connection.push(listen_key_expired_message(event_ts=BASE_TS))
        runtime.pump_once(timeout_s=0.01)

        self.assertIs(recovery.state, RecoveryStatus.NOT_RECOVERED)
        self.assertTrue(any("listenKey" in event for event in runtime.discontinuity_events))

    def test_sc4_stop_auto_invalidates(self) -> None:
        runtime, _fetcher, _factory, recovery, _accounting = self._wired()

        runtime.stop()

        self.assertIs(recovery.state, RecoveryStatus.NOT_RECOVERED)
        self.assertIn("runtime stopped", runtime.discontinuity_events)

    def test_sc5_after_reconnect_recovery_requires_full_rerun(self) -> None:
        runtime, _fetcher, factory, recovery, _accounting = self._wired()

        factory.connection.drop()
        runtime.pump_once(timeout_s=0.01)
        self.assertTrue(runtime.continuity_assumed is False)

        # 断线后自动重连成功，但 continuity 未重建 ⇒ 直接跑 recovery 必须 BLOCKED
        blocked = recovery.run(stream_state=stream_state(continuity=False),
                               snapshot_provider=recovery.fetch_snapshot)
        self.assertIs(blocked.status, RecoveryStatus.BLOCKED)
        self.assertIs(recovery.state, RecoveryStatus.BLOCKED)
        self.assertIsNot(recovery.state, RecoveryStatus.RECOVERED)

        # 走完整流程（重建 boundary + 重新对账）后才允许 RECOVERED
        runtime.refresh_snapshot()
        self.assertTrue(runtime.continuity_assumed)
        again = recovery.run(stream_state=stream_state(), snapshot_provider=recovery.fetch_snapshot)
        self.assertIs(again.status, RecoveryStatus.RECOVERED)

    def test_sc5_live_events_between_disconnect_and_recovery_do_not_restore_state(self) -> None:
        """断线期间到达的业务事件不得让 recovery 自己回到 RECOVERED。"""
        runtime, _fetcher, factory, recovery, _accounting = self._wired()

        factory.connection.drop()
        runtime.pump_once(timeout_s=0.01)
        factory.connection.push(order_update_message(event_ts=BASE_TS + 1))
        runtime.pump_once(timeout_s=0.01)

        self.assertIs(recovery.state, RecoveryStatus.NOT_RECOVERED)

    def test_stream_state_reads_real_runtime_facts(self) -> None:
        runtime, _fetcher, _factory, recovery, _accounting = self._wired()

        from tests.live.test_binance_recovery_live import stream_state_from

        state = stream_state_from(runtime)

        self.assertEqual(state.listen_key_state, "ACTIVE")
        self.assertTrue(state.continuity_assumed)
        self.assertTrue(state.boundary_present)
        self.assertEqual(runtime.latest_snapshot.total_wallet_balance, 1000.50)  # type: ignore[union-attr]
        self.assertEqual(runtime.config.symbol, SYMBOL)

    def test_account_payload_fixture_has_position_entry(self) -> None:
        payload = account_payload()

        self.assertEqual(payload["positions"][0]["symbol"], SYMBOL)


class DiscontinuityListenerTest(unittest.TestCase):
    def test_listener_must_be_callable(self) -> None:
        from connectors.binance.private.errors import PrivateFormatError

        runtime, _fetcher, _factory = build_runtime()
        with self.assertRaises(PrivateFormatError):
            runtime.subscribe_discontinuity("not-callable")  # type: ignore[arg-type]

    def test_multiple_listeners_all_receive_the_reason(self) -> None:
        runtime, _fetcher, factory = build_runtime()
        seen: list[str] = []
        runtime.subscribe_discontinuity(seen.append)
        runtime.subscribe_discontinuity(lambda reason: seen.append(f"second:{reason}"))
        runtime.start()

        factory.connection.drop()
        runtime.pump_once(timeout_s=0.01)

        self.assertEqual(len(seen), 2)
        self.assertEqual(runtime.discontinuity_events, (seen[0],))

    def test_no_discontinuity_event_on_successful_start_and_pump(self) -> None:
        runtime, _fetcher, factory = build_runtime()
        seen: list[str] = []
        runtime.subscribe_discontinuity(seen.append)
        runtime.start()
        factory.connection.push(order_update_message(event_ts=BASE_TS))
        runtime.pump_once(timeout_s=0.01)

        self.assertEqual(seen, [])
        self.assertEqual(runtime.discontinuity_events, ())

    def test_fake_connection_flags(self) -> None:
        connection = FakeConnection()
        connection.push("{}")

        self.assertEqual(connection.recv_text(timeout_s=0.01), "{}")


if __name__ == "__main__":
    unittest.main()
