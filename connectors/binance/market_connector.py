"""Binance market data connector adapter（P0001.15 §10）。

**薄 adapter**：把既有 `LiveMarketDataRuntime`（REST + WS + exchangeInfo/TradingRules + mark price + 时钟）
适配为统一 `MarketDataConnector` contract，**不重写**任何 Binance client。

只暴露公开市场事实；不持有 broker、不下单、不改账户状态。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from market.events.types import Milliseconds
from venue.contracts import MarketDataFacts, MarketTimestamps
from venue.health import ConnectionState, MarketConnectorHealth
from venue.identity import VenueIdentity
from venue.reference_price import MarkPriceReferenceSource


class BinanceMarkPriceReferenceSource(MarkPriceReferenceSource):
    """由既有正式 Binance mark 观测（`markPriceUpdate`）驱动的 MARK source。

    与统一 `MarketEvent.MARK_PRICE` 路径**同一个 source 类**：`latest()` 会把 runtime 的最新正式
    mark 观测并入同一条事实链（`source="binance.mark_price_update"`）。绝不使用 last trade。
    """

    def __init__(self, *, runtime: object, venue_identity: VenueIdentity, instrument_id: str) -> None:
        super().__init__(venue_identity=venue_identity, instrument_id=instrument_id,
                         source="binance.mark_price_update")
        self._runtime = runtime

    def latest(self, *, now_ms: Milliseconds):  # type: ignore[override]
        observation = getattr(self._runtime, "latest_mark", lambda: None)()
        if observation is not None:
            self.observe_mark_price(price=float(observation.price), exchange_ts=int(observation.exchange_ts),
                                    received_at=int(observation.receive_ts), source=self._source_name)
        return super().latest(now_ms=now_ms)


@dataclass
class BinanceMarketDataConnector:
    """`LiveMarketDataRuntime` → `MarketDataConnector`（只读事实投影）。"""

    runtime: object
    venue_identity: VenueIdentity
    instrument_id: str
    data_source: str = "binance:ws"
    connector_id_suffix: str = "market"
    clock: Callable[[], int] = field(default=lambda: 0)
    _connected: bool = field(default=False, init=False)
    _seen_reconnects: int = field(default=0, init=False)
    _reference_source: MarkPriceReferenceSource | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.venue_identity, VenueIdentity):
            raise ValueError("BinanceMarketDataConnector.venue_identity must be a VenueIdentity")
        if not isinstance(self.instrument_id, str) or not self.instrument_id:
            raise ValueError("BinanceMarketDataConnector.instrument_id must be a non-empty string")

    @property
    def connector_id(self) -> str:
        return f"{self.venue_identity.venue_id}:{self.connector_id_suffix}"

    def connect(self) -> None:
        self.runtime.connect()
        self._connected = True
        self._seen_reconnects = self._telemetry().ws_reconnect_count

    def disconnect(self) -> None:
        self.runtime.close()
        self._connected = False

    def trading_rules(self, symbol: str) -> object | None:
        return self.runtime.trading_rules()

    def latest_facts(self, *, now_ms: Milliseconds) -> MarketDataFacts:
        telemetry = self._telemetry()
        state = self._latest_state()
        if state is None:
            return MarketDataFacts(venue_id=self.venue_identity.venue_id, symbol=self._symbol(),
                                   data_source=self.data_source,
                                   book_health=None, tradeable=None)
        price = getattr(state, "price", None)
        quality = getattr(state, "quality", None)
        book_health = getattr(quality, "book_health", None)
        return MarketDataFacts(
            venue_id=self.venue_identity.venue_id, symbol=self._symbol(), data_source=self.data_source,
            best_bid=getattr(price, "best_bid", None), best_ask=getattr(price, "best_ask", None),
            last_trade_price=getattr(getattr(state, "trade", None), "last_price", None),
            last_market_event_ms=None if telemetry.event_lag_ms is None else int(now_ms) - telemetry.event_lag_ms,
            book_health=getattr(book_health, "value", None) or (str(book_health) if book_health else None),
            tradeable=getattr(quality, "tradeable", None))

    def market_timestamps(self, *, now_ms: Milliseconds) -> MarketTimestamps:
        telemetry = self._telemetry()
        lag = telemetry.event_lag_ms
        return MarketTimestamps(
            venue_id=self.venue_identity.venue_id,
            last_event_exchange_ts=None if lag is None else int(now_ms) - int(lag),
            last_event_process_ts=None,
            event_age_ms=None if lag is None else int(lag),
            clock_offset_ms=telemetry.server_time_offset_ms, observed=telemetry.message_count > 0)

    def health(self, *, now_ms: Milliseconds) -> MarketConnectorHealth:
        telemetry = self._telemetry()
        facts = self.latest_facts(now_ms=now_ms)
        if not self._connected:
            state = ConnectionState.FAILED if telemetry.last_error else ConnectionState.DISCONNECTED
        elif telemetry.ws_reconnect_count > self._seen_reconnects:
            state = ConnectionState.RECONNECTING
        elif telemetry.last_error is not None and telemetry.message_count == 0:
            state = ConnectionState.FAILED
        else:
            state = ConnectionState.CONNECTED
        self._seen_reconnects = max(self._seen_reconnects, telemetry.ws_reconnect_count)
        return MarketConnectorHealth(
            venue_id=self.venue_identity.venue_id, connector_id=self.connector_id,
            connection_state=state,
            last_market_event_ms=None if telemetry.event_lag_ms is None
            else int(now_ms) - int(telemetry.event_lag_ms),
            event_age_ms=telemetry.event_lag_ms,
            book_health=facts.book_health, clock_offset_ms=telemetry.server_time_offset_ms,
            reconnect_count=telemetry.ws_reconnect_count, resync_count=telemetry.resync_count,
            data_source=self.data_source, events_observed=telemetry.message_count > 0,
            detail=f"messages={telemetry.message_count} gaps={telemetry.depth_gap_count} "
                   f"resync={telemetry.resync_count} mark_updates={telemetry.mark_update_count}")

    def reference_price_source(self) -> MarkPriceReferenceSource:
        if self._reference_source is None:
            self._reference_source = BinanceMarkPriceReferenceSource(
                runtime=self.runtime, venue_identity=self.venue_identity, instrument_id=self.instrument_id)
        return self._reference_source

    # ------------------------------------------------------------------ 内部

    def _telemetry(self) -> object:
        return self.runtime.telemetry()

    def _latest_state(self) -> object | None:
        history = getattr(self.runtime, "history", None)
        if history is None:
            return None
        states = history()
        return states[-1] if states else None

    def _symbol(self) -> str:
        return self.instrument_id.split(":", 1)[-1]


__all__ = ["BinanceMarketDataConnector", "BinanceMarkPriceReferenceSource"]
