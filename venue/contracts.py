"""Venue Integration contracts（P0001.15 §7–§10、§13、§19 + 人类裁决 4）。

两个**正交**契约，刻意不合并成一个万能 gateway：

- `MarketDataConnector`：公开行情 / 规则 / reference-price source / 健康 / 时钟 —— 只负责**公开市场事实**；
  **不得**持有 broker、下单、修改账户状态或成为 execution owner。
- `PrivateExecutionConnector`：账户 / 持仓 / 下单 / 撤单 / 查询 / 成交 / private stream / reconciliation / 健康 / rate-limit。

执行三分类语义**复用既有 vocabulary**（`ExecutionEvent`）：

```
ORDER_ACCEPTED 事件        → ACCEPTED
ORDER_REJECTED 事件        → REJECTED
两者都没有                 → UNKNOWN（提交结果未知；**不得**自动 retry）
```

数据面 pump（行情推送/重连/重放）仍由既有 owner（`MarketFeedProvider` / `ReplaySource` / Binance market-data runtime）
负责；connector 暴露契约级事实，不复制第二套 pump（§29：禁止大爆炸式重写）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from domain.instruments.model import InstrumentSpec
from execution.events import ExecutionEvent, ExecutionEventType
from execution.types import ExternalFill, ExternalOrder, Order
from market.events.types import Milliseconds
from venue.health import MarketConnectorHealth, PrivateConnectorHealth
from venue.identity import VenueIdentity
from venue.reference_price import ReferencePrice, ReferencePriceProvider


class SubmissionClassification(Enum):
    """一次提交的三分类结果（§8：UNKNOWN 不得自动 retry）。"""

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


def classify_submission(events: tuple[ExecutionEvent, ...] | list[ExecutionEvent]) -> SubmissionClassification:
    """由既有 execution 事件判定三分类（不引入平行词汇）。"""
    for event in events:
        if event.event_type is ExecutionEventType.ORDER_REJECTED:
            return SubmissionClassification.REJECTED
    for event in events:
        if event.event_type is ExecutionEventType.ORDER_ACCEPTED:
            return SubmissionClassification.ACCEPTED
    return SubmissionClassification.UNKNOWN


@dataclass(frozen=True, slots=True)
class MarketDataFacts:
    """connector 级市场事实（只读投影；字段 `None` = UNKNOWN）。"""

    venue_id: str
    symbol: str
    data_source: str = ""
    best_bid: float | None = None
    best_ask: float | None = None
    last_trade_price: float | None = None
    last_market_event_ms: Milliseconds | None = None
    book_health: str | None = None
    tradeable: bool | None = None

    def view(self) -> dict[str, object]:
        return {
            "venue_id": self.venue_id, "symbol": self.symbol, "data_source": self.data_source,
            "best_bid": self.best_bid, "best_ask": self.best_ask,
            "last_trade_price": self.last_trade_price, "last_market_event_ms": self.last_market_event_ms,
            "book_health": self.book_health, "tradeable": self.tradeable,
        }


@dataclass(frozen=True, slots=True)
class MarketTimestamps:
    """行情时间事实（§7 `market timestamps` / data freshness）。"""

    venue_id: str
    last_event_exchange_ts: Milliseconds | None = None
    last_event_process_ts: Milliseconds | None = None
    event_age_ms: int | None = None
    clock_offset_ms: int | None = None
    observed: bool = False

    def view(self) -> dict[str, object]:
        return {
            "venue_id": self.venue_id,
            "last_event_exchange_ts": self.last_event_exchange_ts,
            "last_event_process_ts": self.last_event_process_ts,
            "event_age_ms": self.event_age_ms,
            "clock_offset_ms": self.clock_offset_ms,
            "observed": self.observed,
        }


@runtime_checkable
class ReferencePriceSource(Protocol):
    """reference price source（§11）。"""

    @property
    def source_id(self) -> str: ...

    @property
    def venue_id(self) -> str: ...

    def latest(self, *, now_ms: Milliseconds) -> ReferencePrice: ...


@runtime_checkable
class MarketDataConnector(Protocol):
    """公开市场数据 connector（§7）。绝不拥有 execution 状态。"""

    @property
    def connector_id(self) -> str: ...

    @property
    def venue_identity(self) -> VenueIdentity: ...

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def trading_rules(self, symbol: str) -> object | None: ...

    def latest_facts(self, *, now_ms: Milliseconds) -> MarketDataFacts: ...

    def market_timestamps(self, *, now_ms: Milliseconds) -> MarketTimestamps: ...

    def health(self, *, now_ms: Milliseconds) -> MarketConnectorHealth: ...

    def reference_price_source(self) -> ReferencePriceSource | None: ...


@runtime_checkable
class PrivateExecutionConnector(Protocol):
    """私有执行 connector（§8）。执行三分类语义与既有 execution 事件一致。"""

    @property
    def connector_id(self) -> str: ...

    @property
    def venue_identity(self) -> VenueIdentity: ...

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    # --- execution seam（与既有 `ExecutionAdapter` 相同语义，engine 只依赖这一层）
    def submit(self, order: Order) -> tuple[ExecutionEvent, ...]: ...

    def cancel(self, order: Order) -> tuple[ExecutionEvent, ...]: ...

    def poll(self) -> tuple[ExecutionEvent, ...]: ...

    def open_orders(self) -> tuple[ExternalOrder, ...]: ...

    def recent_fills(self, *, since_ms: Milliseconds | None = None) -> tuple[ExternalFill, ...]: ...

    # --- private facts / health
    def account_snapshot(self) -> object | None: ...

    def positions(self) -> tuple[object, ...]: ...

    def reconciliation_state(self) -> str | None: ...

    def rate_limit_facts(self) -> object | None: ...

    def health(self, *, now_ms: Milliseconds) -> PrivateConnectorHealth: ...


__all__ = [
    "MarketDataConnector", "MarketDataFacts", "MarketTimestamps", "PrivateExecutionConnector",
    "ReferencePriceSource", "ReferencePriceProvider", "SubmissionClassification", "classify_submission",
]
