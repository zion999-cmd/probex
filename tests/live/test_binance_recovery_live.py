"""P0001.9.3 authenticated live acceptance（opt-in，**需要凭据**）：真实读取 openOrders / allOrders / userTrades。

```bash
export BINANCE_API_KEY=...
export BINANCE_API_SECRET=...
export PROBEX_LIVE_PRIVATE=1
python3 -m unittest -v tests.live.test_binance_recovery_live
```

测试网（USDⓈ-M Futures Testnet）通过环境变量覆盖端点：

```bash
export PROBEX_LIVE_REST_BASE=https://demo-fapi.binance.com
export PROBEX_LIVE_WS_HOST=wss://fstream.binancefuture.com
```

覆盖 SC-10：真实读取三只读端点 → 归一化 → `StartupRecovery` 门控 → 明确 `RECOVERED` / `BLOCKED(+reason)`。

安全：全程只读；凭据只从环境变量读取；报告遮蔽敏感值。
"""

from __future__ import annotations

import json
import os
import time
import unittest

from connectors.binance.market_data.endpoints import REST_BASE_URL, WS_HOST
from connectors.binance.market_data.transport import ReconnectPolicy, connect
from connectors.binance.private.auth import ApiCredentials
from connectors.binance.private.recovery import RecoveryStatus, StartupRecovery, StreamState
from connectors.binance.private.rest import PrivateRestClient, UrllibRestFetcher
from connectors.binance.private.runtime import PrivateAccountRuntime
from connectors.binance.private.user_stream import UserStreamClient
from execution.tracker import OrderTracker
from portfolio.accounting import AccountingCore
from tests.live_support import BINANCE_SYMBOL
from tests.private_support import private_config

#: 与 P0001.9.2 相同的 opt-in 开关（同一批真实凭据）。
LIVE_FLAG = "PROBEX_LIVE_PRIVATE"
REST_BASE_ENV = "PROBEX_LIVE_REST_BASE"
WS_HOST_ENV = "PROBEX_LIVE_WS_HOST"
FACT_LIMIT_ENV = "PROBEX_LIVE_RECOVERY_FACT_LIMIT"
DEFAULT_FACT_LIMIT = 50


def rest_base() -> str:
    return os.environ.get(REST_BASE_ENV) or REST_BASE_URL


def ws_host() -> str:
    return os.environ.get(WS_HOST_ENV) or WS_HOST


def build_live_recovery(*, base_url: str | None = None, host: str | None = None):
    """组装（runtime, recovery, tracker, accounting）；凭据缺失时 fail closed。"""
    clock = lambda: int(time.time() * 1000)  # noqa: E731
    credentials = ApiCredentials.from_env()
    rest = PrivateRestClient(
        credentials=credentials,
        fetcher=UrllibRestFetcher(),
        base_url=base_url or rest_base(),
        recv_window_ms=5_000,
        timeout_s=15.0,
        clock=clock,
    )
    runtime = PrivateAccountRuntime(
        config=private_config(
            symbol=BINANCE_SYMBOL,
            max_median_private_lag_ms=int(os.environ.get("PROBEX_LIVE_PRIVATE_MAX_LAG_MS", "2000")),
            reconnect=ReconnectPolicy(max_attempts=3, base_backoff_ms=500, max_backoff_ms=4_000),
        ),
        rest=rest,
        stream=UserStreamClient(
            transport_factory=lambda url, timeout_s: connect(url, timeout_s=timeout_s),
            ws_host=host or ws_host(),
        ),
        credentials=credentials,
        clock=clock,
    )
    tracker = OrderTracker(session_id="live-recovery")
    accounting = AccountingCore(initial_balance=0.0)
    recovery = StartupRecovery(
        rest=rest,
        tracker=tracker,
        accounting=accounting,
        symbol=BINANCE_SYMBOL,
        clock=clock,
        fact_limit=int(os.environ.get(FACT_LIMIT_ENV, DEFAULT_FACT_LIMIT)),
    )
    return runtime, recovery, tracker, accounting


def stream_state_from(runtime: PrivateAccountRuntime) -> StreamState:
    """由**真实** runtime 事实构造恢复门输入（不伪造 ACTIVE / continuity）。"""
    return StreamState(
        listen_key_state=runtime.telemetry.listen_key_state,
        continuity_assumed=runtime.continuity_assumed,
        boundary_present=runtime.snapshot_boundary is not None,
    )


def recovery_report(
    *, recovery: StartupRecovery, runtime: PrivateAccountRuntime, result, tracker: OrderTracker,
    accounting: AccountingCore, elapsed_ms: int,
) -> dict:
    """构造可审计报告（只含计数与状态，不含任何凭据/签名）。"""
    snapshot = result.snapshot
    return {
        "environment": {
            "rest_base": rest_base(),
            "ws_host": ws_host(),
            "symbol": BINANCE_SYMBOL,
            "note": "只读端点（openOrders/allOrders/userTrades），无下单/撤单",
        },
        "stream": {
            "listen_key_state": runtime.telemetry.listen_key_state,
            "continuity_assumed": runtime.continuity_assumed,
            "boundary_present": runtime.snapshot_boundary is not None,
            "heartbeats": runtime.telemetry.heartbeat_count,
        },
        "recovery": {
            "status": result.status.value,
            "reasons": [reason.value for reason in result.reasons],
            "detail": result.detail,
            "elapsed_ms": elapsed_ms,
        },
        "facts": None
        if snapshot is None
        else {
            "captured_at": snapshot.captured_at,
            "probex_open_orders": len(snapshot.open_orders),
            "probex_history_orders": len(snapshot.history),
            "probex_fills": len(snapshot.fills),
            "foreign_open_orders": len(snapshot.foreign_open_orders),
            "foreign_history_ignored": snapshot.foreign_ignored,
            "unresolved_fill_orders": snapshot.unresolved_fill_orders,
            "position_amt": snapshot.position.position_amt,
            "position_side": snapshot.position.position_side,
        },
        "accounting": {
            "baseline_applied": accounting.baseline_applied,
            "baseline_source": accounting.baseline.source if accounting.baseline else None,
            "baseline_position_qty": accounting.baseline.position_qty if accounting.baseline else None,
            "synthetic_fills": len(accounting.fills.fills),
            "historical_pnl_known": accounting.historical_pnl_known,
        },
        "tracker": {
            "orders": len(tracker.orders),
            "uncertain_orders": [order.client_order_id for order in tracker.uncertain_orders()],
            "unresolved_orders": [entry.client_order_id for entry in tracker.unresolved_orders()],
            "confirmed_open_exposure": tracker.confirmed_open_exposure(),
        },
    }


@unittest.skipUnless(os.environ.get(LIVE_FLAG) == "1", f"set {LIVE_FLAG}=1 to run the authenticated live recovery")
class BinanceRecoveryLiveTest(unittest.TestCase):
    def test_credentials_are_required(self) -> None:
        from connectors.binance.private.errors import CredentialsError

        if not os.environ.get("BINANCE_API_KEY"):
            with self.assertRaises(CredentialsError):
                ApiCredentials.from_env()

    def test_sc10_real_read_and_gate(self) -> None:
        runtime, recovery, tracker, accounting = build_live_recovery()
        try:
            runtime.start()  # 真实 listenKey + user stream + snapshot boundary（P0001.9.2 纪律）
            state = stream_state_from(runtime)
            started = int(time.time() * 1000)
            result = recovery.run(stream_state=state, snapshot_provider=recovery.fetch_snapshot)
            elapsed_ms = int(time.time() * 1000) - started
            report = recovery_report(
                recovery=recovery, runtime=runtime, result=result, tracker=tracker,
                accounting=accounting, elapsed_ms=elapsed_ms,
            )
            print("\nPROBEX RECOVERY LIVE REPORT:\n" + json.dumps(report, indent=2, ensure_ascii=False))
        finally:
            runtime.stop()

        # SC-10：三只读端点真实读取成功（RECOVERED 时 facts 必然存在）
        self.assertIn(result.status, {RecoveryStatus.RECOVERED, RecoveryStatus.BLOCKED})
        if result.status is RecoveryStatus.BLOCKED:
            self.assertTrue(result.reasons, "BLOCKED must carry at least one reason code")
            self.skipTest(f"recovery BLOCKED: {[reason.value for reason in result.reasons]}")
        self.assertIsNotNone(result.snapshot)
        self.assertTrue(accounting.baseline_applied)
        self.assertEqual(len(accounting.fills.fills), 0)  # 不产生 synthetic Fill
        if result.snapshot is not None:
            self.assertEqual(result.snapshot.symbol, BINANCE_SYMBOL)
            self.assertAlmostEqual(
                accounting.position(BINANCE_SYMBOL).qty, result.snapshot.position.position_amt
            )


if __name__ == "__main__":
    unittest.main()
