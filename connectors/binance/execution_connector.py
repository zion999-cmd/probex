"""Binance private execution connector adapter（P0001.15 §10）。

**薄 adapter**：把既有 Binance 写执行能力（`BinanceExecutionAdapter` + `PrivateAccountRuntime`）
适配为统一 `PrivateExecutionConnector` contract，**不重写** REST / WS / user stream / account / order /
fills / TradingRules / rate limit / reconciliation / UNKNOWN 语义 / clientOrderId ownership / clock offset。

执行三分类语义不变：接受事件 ⇒ ACCEPTED；拒绝事件 ⇒ REJECTED；两者都没有 ⇒ **UNKNOWN（不得 retry）**。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from execution.events import ExecutionEvent, ExecutionEventType
from execution.types import ExternalFill, ExternalOrder, Order
from market.events.types import Milliseconds
from venue.health import ConnectionState, PrivateConnectorHealth
from venue.identity import VenueIdentity

#: listenKey 生命周期状态 → connector connection state（直接映射，不发明阈值）。
_CONNECTION_STATE_BY_LIFECYCLE: dict[str, ConnectionState] = {
    "STOPPED": ConnectionState.DISCONNECTED,
    "STARTING": ConnectionState.CONNECTING,
    "ACTIVE": ConnectionState.CONNECTED,
    "RENEWING": ConnectionState.CONNECTED,
    "RECONNECTING": ConnectionState.RECONNECTING,
    "EXPIRED": ConnectionState.FAILED,
    "FAILED": ConnectionState.FAILED,
}


@dataclass
class BinancePrivateExecutionConnector:
    """`BinanceExecutionAdapter` + `PrivateAccountRuntime` → `PrivateExecutionConnector`。"""

    adapter: object
    venue_identity: VenueIdentity
    symbol: str
    clock: Callable[[], int]
    runtime: object | None = None
    reconciliation_state_provider: Callable[[], str | None] | None = None
    rate_limit_provider: Callable[[], object | None] | None = None
    connector_id_suffix: str = "execution"
    _connected: bool = field(default=False, init=False)
    _last_event_ms: Milliseconds | None = field(default=None, init=False)
    _last_ack_ms: Milliseconds | None = field(default=None, init=False)
    _event_count: int = field(default=0, init=False)
    _submit_count: int = field(default=0, init=False)
    _reject_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.venue_identity, VenueIdentity):
            raise ValueError("BinancePrivateExecutionConnector.venue_identity must be a VenueIdentity")
        for name in ("submit", "cancel", "poll", "open_orders", "recent_fills"):
            if not hasattr(self.adapter, name):
                raise ValueError(f"BinancePrivateExecutionConnector.adapter must expose {name}()")

    @property
    def connector_id(self) -> str:
        return f"{self.venue_identity.venue_id}:{self.connector_id_suffix}"

    # ------------------------------------------------------------------ lifecycle

    def connect(self) -> None:
        if self.runtime is not None:
            self.runtime.start()
        self._connected = True

    def disconnect(self) -> None:
        if self.runtime is not None:
            self.runtime.stop()
        self._connected = False

    # ------------------------------------------------------------------ execution seam

    def submit(self, order: Order) -> tuple[ExecutionEvent, ...]:
        self._submit_count += 1
        return self._note(self.adapter.submit(order))

    def cancel(self, order: Order) -> tuple[ExecutionEvent, ...]:
        return self._note(self.adapter.cancel(order))

    def poll(self) -> tuple[ExecutionEvent, ...]:
        return self._note(self.adapter.poll())

    def query_order(self, *, client_order_id: str, timestamp: Milliseconds) -> tuple[ExecutionEvent, ...]:
        """`query order`（§2/§3）：用于 UNKNOWN 收敛 / reconciliation（**不**自动 retry 写请求）。"""
        query = getattr(self.adapter, "query_order", None)
        if query is None:
            return ()
        return self._note(tuple(query(client_order_id=client_order_id, timestamp=timestamp)))

    def open_orders(self) -> tuple[ExternalOrder, ...]:
        return tuple(self.adapter.open_orders())

    def recent_fills(self, *, since_ms: Milliseconds | None = None) -> tuple[ExternalFill, ...]:
        return tuple(self.adapter.recent_fills(since_ms=since_ms))

    def _note(self, events: tuple[ExecutionEvent, ...]) -> tuple[ExecutionEvent, ...]:
        now = int(self.clock())
        for event in events:
            self._event_count += 1
            self._last_event_ms = now
            if event.event_type is ExecutionEventType.ORDER_ACCEPTED:
                self._last_ack_ms = now
            elif event.event_type is ExecutionEventType.ORDER_REJECTED:
                self._reject_count += 1
        return tuple(events)

    # ------------------------------------------------------------------ private facts

    def account_snapshot(self) -> object | None:
        if self.runtime is None:
            return None
        return self.runtime.latest_snapshot()

    def positions(self) -> tuple[object, ...]:
        if self.runtime is None:
            return ()
        position = self.runtime.latest_position()
        return () if position is None else (position,)

    def reconciliation_state(self) -> str | None:
        if self.reconciliation_state_provider is None:
            return None
        return self.reconciliation_state_provider()

    def rate_limit_facts(self) -> object | None:
        if self.rate_limit_provider is None:
            return None
        return self.rate_limit_provider()

    def health(self, *, now_ms: Milliseconds) -> PrivateConnectorHealth:
        telemetry = self.runtime.telemetry if self.runtime is not None else None
        if callable(telemetry):      # 兼容 duck-typed double（真实 runtime 是 property）
            telemetry = telemetry()
        raw_state = str(self.runtime.lifecycle_state()) if self.runtime is not None else "STOPPED"
        connection_state = _CONNECTION_STATE_BY_LIFECYCLE.get(raw_state, ConnectionState.UNKNOWN)
        if not self._connected:
            connection_state = ConnectionState.DISCONNECTED
        snapshot = self.account_snapshot()
        captured = getattr(snapshot, "process_ts", None) or getattr(snapshot, "receive_ts", None)
        rate_facts = self.rate_limit_facts()
        rate_state = None
        if rate_facts is not None:
            status = getattr(rate_facts, "status", None) or getattr(rate_facts, "rate_limit_state", None)
            rate_state = str(getattr(status, "value", status)) if status is not None else None
        return PrivateConnectorHealth(
            venue_id=self.venue_identity.venue_id, connector_id=self.connector_id,
            connection_state=connection_state,
            last_private_event_ms=self._last_event_ms,
            private_event_age_ms=(None if self._last_event_ms is None
                                  else max(0, int(now_ms) - self._last_event_ms)),
            last_order_ack_ms=self._last_ack_ms,
            reconciliation_state=self.reconciliation_state(),
            account_state_freshness_ms=(None if not isinstance(captured, int)
                                        else max(0, int(now_ms) - captured)),
            rate_limit_state=rate_state,
            uncertain_order_count=None,
            private_events_observed=bool(telemetry is not None and telemetry.message_count > 0),
            detail=(f"listen_key={raw_state} events={self._event_count} "
                    f"submits={self._submit_count} rejects={self._reject_count}"
                    + ("" if telemetry is None else
                       f" messages={telemetry.message_count} "
                       f"lag_ms={telemetry.last_receive_lag_ms} offset_ms={telemetry.server_time_offset_ms}")))

    def counters(self) -> dict[str, int]:
        return {"events": self._event_count, "submits": self._submit_count, "rejects": self._reject_count}


__all__ = ["BinancePrivateExecutionConnector"]
