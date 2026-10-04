"""Connector health（P0001.15 §16–§18 + 人类裁决 4）：market 与 private 的健康状态**必须独立**。

结构性纪律：

- 不存在把两者合并成一个 `connected` 的 API（SC-28）；
- 未观测到 private 事件时**不得**推断「没有成交」——事件类字段保持 `None` + `events_observed=False`；
- 所有事实字段 `None` = UNKNOWN（不是 0）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from market.events.types import Milliseconds


class ConnectionState(Enum):
    """连接状态（UNKNOWN 是一等状态，不得当成断开或已连接）。"""

    UNKNOWN = "UNKNOWN"
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class MarketConnectorHealth:
    """公开市场连接健康（§17）。"""

    venue_id: str
    connection_state: ConnectionState
    connector_id: str = ""
    last_market_event_ms: Milliseconds | None = None
    event_age_ms: int | None = None
    book_health: str | None = None
    clock_offset_ms: int | None = None
    reconnect_count: int | None = None
    resync_count: int | None = None
    data_source: str | None = None
    events_observed: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.connection_state, ConnectionState):
            raise ValueError("MarketConnectorHealth.connection_state must be a ConnectionState")
        if not isinstance(self.venue_id, str) or not self.venue_id:
            raise ValueError("MarketConnectorHealth.venue_id must be a non-empty string")
        if not self.events_observed:
            # 没有观测到任何市场事件 ⇒ 事件类事实必须保持 UNKNOWN
            object.__setattr__(self, "last_market_event_ms", None)
            object.__setattr__(self, "event_age_ms", None)

    def view(self) -> dict[str, object]:
        return {
            "connector_id": self.connector_id,
            "venue_id": self.venue_id,
            "connection_state": self.connection_state.value,
            "last_market_event_ms": self.last_market_event_ms,
            "event_age_ms": self.event_age_ms,
            "book_health": self.book_health,
            "clock_offset_ms": self.clock_offset_ms,
            "reconnect_count": self.reconnect_count,
            "resync_count": self.resync_count,
            "data_source": self.data_source,
            "events_observed": self.events_observed,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class PrivateConnectorHealth:
    """私有执行连接健康（§18）。"""

    venue_id: str
    connection_state: ConnectionState
    connector_id: str = ""
    last_private_event_ms: Milliseconds | None = None
    private_event_age_ms: int | None = None
    last_order_ack_ms: Milliseconds | None = None
    reconciliation_state: str | None = None
    account_state_freshness_ms: int | None = None
    rate_limit_state: str | None = None
    uncertain_order_count: int | None = None
    private_events_observed: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.connection_state, ConnectionState):
            raise ValueError("PrivateConnectorHealth.connection_state must be a ConnectionState")
        if not isinstance(self.venue_id, str) or not self.venue_id:
            raise ValueError("PrivateConnectorHealth.venue_id must be a non-empty string")
        if not self.private_events_observed:
            # 未收到 private 事件 ⇒ 不得推断"没有成交"；事件类事实保持 UNKNOWN
            object.__setattr__(self, "last_private_event_ms", None)
            object.__setattr__(self, "private_event_age_ms", None)

    def view(self) -> dict[str, object]:
        return {
            "connector_id": self.connector_id,
            "venue_id": self.venue_id,
            "connection_state": self.connection_state.value,
            "last_private_event_ms": self.last_private_event_ms,
            "private_event_age_ms": self.private_event_age_ms,
            "last_order_ack_ms": self.last_order_ack_ms,
            "reconciliation_state": self.reconciliation_state,
            "account_state_freshness_ms": self.account_state_freshness_ms,
            "rate_limit_state": self.rate_limit_state,
            "uncertain_order_count": self.uncertain_order_count,
            "private_events_observed": self.private_events_observed,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    """两个 connector 的**并列**健康（刻意不提供单一 `connected` 布尔）。"""

    market: MarketConnectorHealth
    private: PrivateConnectorHealth

    @property
    def diverges(self) -> bool:
        """两者状态是否不同（SC-28 的核心事实）。"""
        return self.market.connection_state is not self.private.connection_state

    def view(self) -> dict[str, object]:
        return {"market": self.market.view(), "private": self.private.view(),
                "diverges": self.diverges}


__all__ = ["ConnectionState", "ConnectorHealth", "MarketConnectorHealth", "PrivateConnectorHealth"]
