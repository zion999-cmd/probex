"""PAPER execution connector（P0001.15 §9）：把唯一 `PaperBroker` 适配为统一 connector contract。

链路（§9）：

```
ExecutionEngine → PrivateExecutionConnector → PaperExecutionConnector → PaperBroker
```

纪律：

- **唯一** `PaperBroker` 实例由本 connector 持有；`PaperBroker` 不直接暴露给 Strategy / Product / UI；
- 本 adapter 只做转发 + 事实记录（自身调用产生的时间戳），**不做**价格 / 风险 / 生命周期判断；
- 账户 / 持仓事实来自注入的 provider（accounting owner），缺 provider ⇒ 如实 `None`（UNKNOWN，不是 0）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from execution.adapters.base import ExecutionAdapter
from execution.events import ExecutionEvent, ExecutionEventType
from execution.types import ExternalFill, ExternalOrder, Order
from market.events.types import Milliseconds
from venue.health import ConnectionState, PrivateConnectorHealth
from venue.identity import VenueIdentity


@dataclass
class PaperExecutionConnector:
    """`ExecutionAdapter` + private connector contract 的 PAPER 实现。"""

    broker: ExecutionAdapter
    venue_identity: VenueIdentity
    symbol: str
    clock: Callable[[], int]
    #: 账户 / 持仓事实（accounting owner 的只读投影；缺 ⇒ UNKNOWN）
    account_provider: Callable[[], object | None] | None = None
    position_provider: Callable[[str], object | None] | None = None
    unresolved_order_count_provider: Callable[[], int] | None = None
    connector_id_suffix: str = "execution"
    _connected: bool = field(default=False, init=False)
    _last_event_ms: Milliseconds | None = field(default=None, init=False)
    _last_ack_ms: Milliseconds | None = field(default=None, init=False)
    _event_count: int = field(default=0, init=False)
    _submit_count: int = field(default=0, init=False)
    _cancel_count: int = field(default=0, init=False)
    _reject_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.venue_identity, VenueIdentity):
            raise ValueError("PaperExecutionConnector.venue_identity must be a VenueIdentity")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("PaperExecutionConnector.symbol must be a non-empty string")
        for name in ("submit", "cancel", "poll", "open_orders", "recent_fills"):
            if not hasattr(self.broker, name):
                raise ValueError(f"PaperExecutionConnector.broker must expose {name}()")

    # ------------------------------------------------------------------ identity

    @property
    def connector_id(self) -> str:
        return f"{self.venue_identity.venue_id}:{self.connector_id_suffix}"

    # ------------------------------------------------------------------ lifecycle

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ execution seam

    def submit(self, order: Order) -> tuple[ExecutionEvent, ...]:
        self._submit_count += 1
        return self._note(self.broker.submit(order))

    def cancel(self, order: Order) -> tuple[ExecutionEvent, ...]:
        self._cancel_count += 1
        return self._note(self.broker.cancel(order))

    def poll(self) -> tuple[ExecutionEvent, ...]:
        return self._note(self.broker.poll())

    def open_orders(self) -> tuple[ExternalOrder, ...]:
        return tuple(self.broker.open_orders())

    def recent_fills(self, *, since_ms: Milliseconds | None = None) -> tuple[ExternalFill, ...]:
        return tuple(self.broker.recent_fills(since_ms=since_ms))

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
        if self.account_provider is None:
            return None
        return self.account_provider()

    def positions(self) -> tuple[object, ...]:
        if self.position_provider is None:
            return ()
        position = self.position_provider(self.symbol)
        return () if position is None else (position,)

    def reconciliation_state(self) -> str | None:
        """PAPER 没有交易所侧 reconciliation（如实 UNKNOWN，不伪造 `RECONCILED`）。"""
        return None

    def rate_limit_facts(self) -> object | None:
        """PAPER 没有 venue rate limit 事实。"""
        return None

    def health(self, *, now_ms: Milliseconds) -> PrivateConnectorHealth:
        account_freshness: int | None = None
        snapshot = self.account_snapshot()
        captured = getattr(snapshot, "captured_at", None) or getattr(snapshot, "process_ts", None)
        if isinstance(captured, int):
            account_freshness = max(0, int(now_ms) - captured)
        unresolved = None
        if self.unresolved_order_count_provider is not None:
            unresolved = int(self.unresolved_order_count_provider())
        return PrivateConnectorHealth(
            venue_id=self.venue_identity.venue_id, connector_id=self.connector_id,
            connection_state=(ConnectionState.CONNECTED if self._connected
                              else ConnectionState.DISCONNECTED),
            last_private_event_ms=self._last_event_ms,
            private_event_age_ms=(None if self._last_event_ms is None
                                  else max(0, int(now_ms) - self._last_event_ms)),
            last_order_ack_ms=self._last_ack_ms,
            reconciliation_state=None, account_state_freshness_ms=account_freshness,
            rate_limit_state=None, uncertain_order_count=unresolved,
            private_events_observed=self._event_count > 0,
            detail=f"paper events={self._event_count} submits={self._submit_count} "
                   f"cancels={self._cancel_count} rejects={self._reject_count}")

    def counters(self) -> dict[str, int]:
        return {"events": self._event_count, "submits": self._submit_count,
                "cancels": self._cancel_count, "rejects": self._reject_count}


__all__ = ["PaperExecutionConnector"]
