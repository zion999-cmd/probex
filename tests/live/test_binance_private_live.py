"""P0001.9.2 authenticated live smoke（opt-in，**需要凭据**）。

```bash
export BINANCE_API_KEY=...        # 只放在当前 shell 环境里
export BINANCE_API_SECRET=...
export PROBEX_LIVE_PRIVATE=1
python3 -m unittest -v tests.live.test_binance_private_live
```

覆盖：

- SC-4：真实账户 / 持仓快照（balance / position / liquidation / leverage）；
- SC-6：真实 user data stream 连接并持续收到事件（无业务事件时以心跳/连接存活 + 超时计数证明活着）；
- SC-9：keepalive 续期（观察 `keepalive_count`，或至少证明 listenKey 未过期）；
- SC-12：private stream receive-lag 的 median / p95 / max 真实测量；
- SC-13：median 超阈值 ⇒ 报告 BLOCKED（不进入真实下单阶段）。

安全：本测试**只读**；凭据只从环境变量读取，不写入任何文件/输出；打印报告前会遮蔽敏感值。
"""

from __future__ import annotations

import json
import os
import time
import unittest

from connectors.binance.market_data.endpoints import LISTEN_KEY_PATH, REST_BASE_URL
from connectors.binance.market_data.transport import ReconnectPolicy
from connectors.binance.private.auth import API_KEY_ENV, API_SECRET_ENV, ApiCredentials, ServerTimeOffset
from connectors.binance.private.errors import CredentialsError
from connectors.binance.private.rest import PrivateRestClient, UrllibRestFetcher
from connectors.binance.private.runtime import PrivateAccountRuntime, latency_summary
from connectors.binance.private.user_stream import UserStreamClient
from tests.live_support import BINANCE_SYMBOL

#: 需要人类显式打开（避免默认使用真实凭据）。
LIVE_FLAG = "PROBEX_LIVE_PRIVATE"
#: 观察窗口（秒）与默认 lag 阈值（毫秒）。
WINDOW_SECONDS = 60.0
MAX_MEDIAN_LAG_MS = 2_000


@unittest.skipUnless(os.environ.get(LIVE_FLAG) == "1", f"set {LIVE_FLAG}=1 to run the authenticated live smoke")
class BinancePrivateLiveTest(unittest.TestCase):
    def _runtime(self) -> PrivateAccountRuntime:
        from connectors.binance.market_data.endpoints import WS_HOST
        from connectors.binance.market_data.transport import connect

        credentials = ApiCredentials.from_env()  # 缺凭据会抛 CredentialsError（fail closed）
        clock = lambda: int(time.time() * 1000)  # noqa: E731
        rest = PrivateRestClient(
            credentials=credentials,
            fetcher=UrllibRestFetcher(),
            base_url=REST_BASE_URL,
            recv_window_ms=5_000,
            timeout_s=10.0,
            clock=clock,
        )
        from tests.private_support import private_config

        config = private_config(
            symbol=BINANCE_SYMBOL,
            max_median_private_lag_ms=int(os.environ.get("PROBEX_LIVE_PRIVATE_MAX_LAG_MS", MAX_MEDIAN_LAG_MS)),
            reconnect=ReconnectPolicy(max_attempts=3, base_backoff_ms=500, max_backoff_ms=4_000),
        )
        return PrivateAccountRuntime(
            config=config,
            rest=rest,
            stream=UserStreamClient(
                transport_factory=lambda url, timeout_s: connect(url, timeout_s=timeout_s), ws_host=WS_HOST
            ),
            credentials=credentials,
            clock=clock,
        )

    def test_credentials_are_required_and_never_echoed(self) -> None:
        missing = {name: os.environ.get(name) for name in (API_KEY_ENV, API_SECRET_ENV)}

        self.assertTrue(all(missing.values()), "both credential variables must be present for the live smoke")
        os.environ.pop(API_KEY_ENV, None)
        os.environ.pop(API_SECRET_ENV, None)
        try:
            with self.assertRaises(CredentialsError):
                ApiCredentials.from_env()
        finally:
            for name, value in missing.items():
                if value is not None:
                    os.environ[name] = value

    def test_sc4_sc6_sc9_sc12_sc13_private_live_smoke(self) -> None:
        runtime = self._runtime()
        started = runtime.start()
        self.addCleanup(runtime.stop)
        events = _collect(runtime, window_seconds=WINDOW_SECONDS)
        report = _build_report(runtime, boundary=started, events=events)
        print("\nPROBEX PRIVATE LIVE SMOKE REPORT:\n" + json.dumps(report, indent=2, ensure_ascii=False))

        telemetry = runtime.telemetry
        self.assertIsNotNone(runtime.latest_snapshot)
        self.assertIsNotNone(runtime.latest_position)
        self.assertEqual(telemetry.listen_key_state, "ACTIVE")
        self.assertIsNotNone(telemetry.server_time_offset_ms)
        self.assertEqual(telemetry.malformed_count, 0)
        self.assertEqual(telemetry.disconnect_count, 0)
        _assert_sc13(runtime, report, case=self)


def _collect(runtime: PrivateAccountRuntime, *, window_seconds: float) -> int:
    """观察窗口内持续消费事件，返回业务事件条数。"""
    deadline = time.monotonic() + window_seconds
    events = 0
    while time.monotonic() < deadline:
        batch = runtime.pump_once(timeout_s=runtime.config.read_timeout_s, max_messages=16)
        events += len(batch.events)
    return events


def _build_report(runtime: PrivateAccountRuntime, *, boundary, events: int) -> dict:
    telemetry = runtime.telemetry
    snapshot = runtime.latest_snapshot
    position = runtime.latest_position
    return {
        "rest_base_url": REST_BASE_URL,
        "listen_key_endpoint": LISTEN_KEY_PATH,
        "symbol": runtime.config.symbol,
        "snapshot_boundary": {
            "stream_connected_at_ms": boundary.stream_connected_at_ms,
            "account_received_ts": boundary.account_received_ts,
            "position_received_ts": boundary.position_received_ts,
        },
        "account": {
            "can_trade": snapshot.can_trade if snapshot else None,
            "settlement_balance": snapshot.settlement_balance.balance if snapshot and snapshot.settlement_balance else None,
            "total_wallet_balance": snapshot.total_wallet_balance if snapshot else None,
        },
        "position": (
            {
                "position_amt": position.position_amt,
                "entry_price": position.entry_price,
                "mark_price": position.mark_price,
                "liquidation_price": position.liquidation_price,
                "leverage": position.leverage,
                "margin_type": position.margin_type,
            }
            if position
            else None
        ),
        "server_time_offset_ms": telemetry.server_time_offset_ms,
        "private_lag_ms": latency_summary(telemetry.private_lag_ms),
        "snapshot_round_trip_ms": telemetry.snapshot_round_trip_ms,
        "lag_basis": "business_event" if telemetry.private_lag_ms is not None else "path_rtt",
        "listen_key_state": telemetry.listen_key_state,
        "continuity_assumed": telemetry.continuity_assumed,
        "counts": {
            "messages": telemetry.message_count,
            "heartbeats_or_timeouts": telemetry.timeout_count,
            "account_updates": telemetry.account_update_count,
            "order_updates": telemetry.order_update_count,
            "fills": telemetry.fill_observation_count,
            "keepalives": telemetry.keepalive_count,
            "duplicates": telemetry.duplicate_count,
            "out_of_order": telemetry.out_of_order_count,
            "malformed": telemetry.malformed_count,
            "reconnects": telemetry.reconnect_count,
        },
        "events_consumed": events,
        "window_seconds": WINDOW_SECONDS,
        "lag_within_threshold": runtime.lag_within_threshold(),
        "last_error": telemetry.last_error,
    }


def _assert_sc13(runtime: PrivateAccountRuntime, report: dict, *, case: unittest.TestCase) -> None:
    """SC-13：优先业务事件 lag；窗口内无业务事件时退化为私有路径 RTT（明确标注 lag_basis）。"""
    threshold = runtime.config.max_median_private_lag_ms
    if runtime.telemetry.private_lag_ms is not None:
        case.assertTrue(
            runtime.lag_within_threshold(),
            f"median lag exceeds {threshold}ms: {report['private_lag_ms']}",
        )
        return
    round_trip = runtime.telemetry.snapshot_round_trip_ms or 0
    case.assertLessEqual(round_trip, threshold, f"private path RTT {round_trip}ms exceeds {threshold}ms")


if __name__ == "__main__":
    unittest.main()
