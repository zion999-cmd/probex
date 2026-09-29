"""Market feed provider（closure Slice 2）。

**边界**：这里只做"把既有 Owner 的事实喂给既有投影"，不实现任何市场算法：

- 时间推进仍由 `market/replay/source.py`（`ReplayClock` / `ReplaySource`）拥有；
- 事件进入既有 `MarketBook` 与 `FeatureEngine`；
- 展示事实写入既有 `BoundedMarketHistory`（只接收已有事实）；
- PAPER 的 execution 使用既有 `PaperBroker` / `OrderManager`（本 provider 不驱动策略、不产生订单）；
- 三个最小 adapter（book snapshot / health segment / trade print）仅做**字段搬运**，
  放在本边界而不放进 composition root（assembly）。

没有第二套 replay state machine，也没有第二个 order/accounting 状态。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from execution.adapters.paper import PaperBroker
from execution.manager import OrderManager
from execution.tracker import OrderTracker
from market.book.market_book import MarketBook
from market.book.order_book import BookSide
from market.events.payloads import TradePayload
from market.events.types import Venue
from market.features.engine import FeatureEngine
from market.replay.source import ReplaySource
from product.market_projection import BoundedMarketHistory, MarketProjectionConfig
from product.types import RuntimeMode
from storage.events.reader import JsonlEventReader


class FeedError(RuntimeError):
    """feed 配置/运行契约错误。"""


@dataclass(frozen=True, slots=True)
class FeedConfig:
    """feed 输入（全部显式；由 composition root 从 profile/config 解析后传入）。"""

    event_store: str
    projection: MarketProjectionConfig
    history_capacity: int
    view_depth: int

    def __post_init__(self) -> None:
        if not str(self.event_store):
            raise FeedError("FeedConfig.event_store must be a path")
        if not isinstance(self.projection, MarketProjectionConfig):
            raise FeedError("FeedConfig.projection must be a MarketProjectionConfig")
        for name in ("history_capacity", "view_depth"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise FeedError(f"FeedConfig.{name} must be a positive int")


@dataclass(frozen=True, slots=True)
class _TradePrintFact:
    """TradePayload 的最小展示事实（字段搬运，不做推断）。"""

    ts: int
    price: float
    quantity: float
    aggressor: str


@dataclass(frozen=True, slots=True)
class _BookSnapshotFact:
    ts: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class _HealthSegmentFact:
    ts: int
    health: str
    reason: str
    market_generation: int | None = None


@dataclass(slots=True)
class MarketFeedProvider:
    """真实 feed：消费 event store → MarketBook / FeatureEngine → BoundedMarketHistory。"""

    config: FeedConfig
    venue: Venue
    symbol: str
    mode: RuntimeMode
    run_id: str
    clock: Callable[[], int]
    history: BoundedMarketHistory = field(init=False)
    book: MarketBook = field(init=False)
    engine: FeatureEngine = field(init=False)
    paper_broker: PaperBroker | None = field(default=None, init=False)
    paper_manager: OrderManager | None = field(default=None, init=False)
    #: composition root 注入：真实账户采样（复用既有 accounting 事实）
    account_provider: Callable[[int], object | None] | None = None
    history_account: object | None = None
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _stats: dict[str, object] = field(default_factory=dict, init=False)
    _error: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RuntimeMode):
            raise FeedError("MarketFeedProvider.mode must be a RuntimeMode")
        self.history = BoundedMarketHistory(capacity=self.config.history_capacity, run_id=self.run_id)
        self.book = MarketBook(self.venue, self.symbol)
        self.engine = FeatureEngine(self.venue, self.symbol)
        if self.mode is RuntimeMode.PAPER:
            # 真实纸面执行链路就位（无策略触发时不会产生任何订单）
            self.paper_broker = PaperBroker()
            self.paper_manager = OrderManager(tracker=OrderTracker(session_id=self.run_id),
                                              adapter=self.paper_broker)

    # ------------------------------------------------------------------ 事实（只读）

    @property
    def stats(self) -> dict[str, object]:
        return dict(self._stats)

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def last_state(self) -> object | None:
        return self._stats.get("last_state")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def data_timestamp_ms(self) -> int | None:
        value = self._stats.get("last_ts")
        return None if value is None else int(value)  # type: ignore[arg-type]

    def counts(self) -> dict[str, int]:
        return dict(self.history.counts)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None:
            raise FeedError("feed already started")
        self._thread = threading.Thread(target=self._consume, name=f"probex-feed-{self.run_id}",
                                        daemon=True)
        self._thread.start()

    def stop(self, *, timeout_s: float = 5.0) -> None:
        """请求停止并等待（幂等）：事件已被消费的部分保留为事实，不伪造完成。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)

    # ------------------------------------------------------------------ 内部

    def _consume(self) -> None:
        try:
            source = ReplaySource(JsonlEventReader(Path(self.config.event_store)))
            for event in source.iter_events():
                if self._stop.is_set():
                    self._stats["stopped_early"] = True
                    return
                self.book.on_market_event(event)
                state = self.engine.on_market_event(event)
                self.history.feed_snapshot(self._book_snapshot(event.exchange_ts))
                self.history.feed_state(state)
                # 账户采样：只转发既有 AccountingFactsProvider 的事实（不重算、不伪造 0、失败不打断 feed）
                if self.account_provider is not None and self.history_account is not None:
                    try:
                        sample = self.account_provider(self.clock())
                    except Exception as exc:  # noqa: BLE001 - 采样失败不得打断 market feed
                        self._stats["account_sampling_errors"] = int(
                            self._stats.get("account_sampling_errors", 0)) + 1
                        self._stats["account_sampling_last_error"] = type(exc).__name__
                        sample = None
                    if sample is not None:
                        self.history_account.feed(sample)
                transition = self.book.last_transition
                if transition is not None:
                    self.history.feed_health(self._health_segment(transition, event.exchange_ts))
                payload = event.payload
                if isinstance(payload, TradePayload):
                    self.history.feed_trade(self._trade_print(payload, event.exchange_ts))
                self._stats.update({"events": int(self._stats.get("events", 0)) + 1,
                                   "last_ts": int(event.exchange_ts),
                                   "last_state": state,
                                   "last_health": self.book.health.value})
            self._stats["completed"] = True
        except Exception as exc:  # noqa: BLE001 - feed 失败必须显式可见（不静默）
            self._error = f"{type(exc).__name__}: {exc}"
            self._stats["error"] = self._error

    # --- 三个最小 adapter：字段搬运（复用既有对象），不重新实现任何算法 ---

    def _book_snapshot(self, exchange_ts: int) -> _BookSnapshotFact:
        view = self.book.view(self.config.view_depth)
        return _BookSnapshotFact(ts=int(exchange_ts),
                                 bids=tuple((level.price, level.size) for level in view.bids),
                                 asks=tuple((level.price, level.size) for level in view.asks))

    def _health_segment(self, transition: object, exchange_ts: int) -> _HealthSegmentFact:
        to_health = getattr(transition, "to_health", None)
        return _HealthSegmentFact(ts=int(exchange_ts),
                                  health=str(getattr(to_health, "value", to_health)),
                                  reason=str(getattr(transition, "reason", "") or ""),
                                  market_generation=int(self._stats.get("generation", 0) or 0))

    def _trade_print(self, payload: TradePayload, exchange_ts: int) -> _TradePrintFact:
        return _TradePrintFact(ts=int(exchange_ts), price=float(payload.price),
                               quantity=float(payload.quantity),
                               aggressor=str(getattr(payload.aggressor, "value", payload.aggressor)))


__all__ = ["FeedConfig", "FeedError", "MarketFeedProvider"]
