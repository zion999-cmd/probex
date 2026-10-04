"""PAPER / REPLAY 的 market data connector（P0001.15 §7、§28）。

数据面仍由既有 owner 交付（`MarketFeedProvider` / `ReplaySource` / `MarketBook` / `FeatureEngine`）；
本 adapter 只把**只读事实**暴露为统一 `MarketDataConnector` contract，不拥有 execution、不复制 pump。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from market.events.types import Milliseconds
from venue.contracts import MarketDataFacts, MarketTimestamps
from venue.health import ConnectionState, MarketConnectorHealth
from venue.identity import VenueIdentity
from venue.reference_price import MarkPriceReferenceSource


@dataclass
class PaperMarketDataConnector:
    """本地（event store / replay）行情事实的连接器投影。"""

    venue_identity: VenueIdentity
    symbol: str
    clock: Callable[[], int]
    data_source: str
    state_provider: Callable[[], object | None]
    rules_provider: Callable[[], object | None] | None = None
    last_event_provider: Callable[[], tuple[int, int] | None] | None = None
    reference_source: MarkPriceReferenceSource | None = None
    connector_id_suffix: str = "market"
    _connected: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.venue_identity, VenueIdentity):
            raise ValueError("PaperMarketDataConnector.venue_identity must be a VenueIdentity")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("PaperMarketDataConnector.symbol must be a non-empty string")
        if not isinstance(self.data_source, str) or not self.data_source:
            raise ValueError("PaperMarketDataConnector.data_source must be a non-empty string")

    @property
    def connector_id(self) -> str:
        return f"{self.venue_identity.venue_id}:{self.connector_id_suffix}"

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def trading_rules(self, symbol: str) -> object | None:
        if self.rules_provider is None:
            return None
        return self.rules_provider()

    def latest_facts(self, *, now_ms: Milliseconds) -> MarketDataFacts:
        state = self.state_provider()
        if state is None:
            return MarketDataFacts(venue_id=self.venue_identity.venue_id, symbol=self.symbol,
                                   data_source=self.data_source)
        price = getattr(state, "price", None)
        quality = getattr(state, "quality", None)
        stamps = self._timestamps()
        return MarketDataFacts(
            venue_id=self.venue_identity.venue_id, symbol=self.symbol, data_source=self.data_source,
            best_bid=getattr(price, "best_bid", None), best_ask=getattr(price, "best_ask", None),
            last_trade_price=getattr(getattr(state, "trade", None), "last_price", None),
            last_market_event_ms=None if stamps is None else stamps[0],
            book_health=getattr(getattr(quality, "book_health", None), "value", None)
            or (None if quality is None else str(getattr(quality, "book_health", "")) or None),
            tradeable=getattr(quality, "tradeable", None))

    def market_timestamps(self, *, now_ms: Milliseconds) -> MarketTimestamps:
        stamps = self._timestamps()
        if stamps is None:
            return MarketTimestamps(venue_id=self.venue_identity.venue_id, observed=False)
        exchange_ts, process_ts = stamps
        return MarketTimestamps(
            venue_id=self.venue_identity.venue_id, last_event_exchange_ts=exchange_ts,
            last_event_process_ts=process_ts, event_age_ms=max(0, int(now_ms) - process_ts),
            clock_offset_ms=None, observed=True)

    def health(self, *, now_ms: Milliseconds) -> MarketConnectorHealth:
        stamps = self._timestamps()
        facts = self.latest_facts(now_ms=now_ms)
        return MarketConnectorHealth(
            venue_id=self.venue_identity.venue_id, connector_id=self.connector_id,
            connection_state=(ConnectionState.CONNECTED if self._connected
                              else ConnectionState.DISCONNECTED),
            last_market_event_ms=None if stamps is None else stamps[0],
            event_age_ms=None if stamps is None else max(0, int(now_ms) - stamps[1]),
            book_health=facts.book_health, clock_offset_ms=None, reconnect_count=0, resync_count=None,
            data_source=self.data_source, events_observed=stamps is not None,
            detail=f"local market data source={self.data_source}")

    def reference_price_source(self) -> MarkPriceReferenceSource | None:
        return self.reference_source

    def _timestamps(self) -> tuple[int, int] | None:
        if self.last_event_provider is None:
            return None
        return self.last_event_provider()


__all__ = ["PaperMarketDataConnector"]
