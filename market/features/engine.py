"""FeatureEngine：`MarketEvent` → `MarketBook` → `MarketState`。

职责与边界：

- 拥有一个 `MarketBook`，是「盘口事实 → 市场状态」的唯一入口。
- 盘口健康的判定只在这里做一次：不健康时把 `view` 置为 `None`，
  使所有 book 派生 feature 显式变成 unavailable（不沿用旧值）。
- 不 import Jev / Strategy / Execution / Portfolio / Risk，也不做任何交易决策。
- 时间轴只使用事件时间（`exchange_ts` / `receive_ts`），不使用 wall-clock。

P0001.3 §9 的 `book_age_ms` 定义：`as_of_exchange_ts - 最近一次成功应用盘口更新的 exchange_ts`
（不可能为负，回退时间戳按 0 处理）。
"""

from __future__ import annotations

from market.book.market_book import BookUpdate, MarketBook
from market.events.types import MarketEvent, Milliseconds, Venue
from market.features.depth import DEPTH_LEVELS, compute_depth_features
from market.features.flow import FlowFeaturesCalculator
from market.features.trade import TradeFeatureAccumulator
from market.features.price import compute_price_features
from market.features.returns import (
    HISTORY_WINDOW_MS,
    HISTORY_WINDOW_MS,
    PRICE_HISTORY_HORIZON_MS,
    compute_returns,
)
from market.features.volatility import UNAVAILABLE_VOLATILITY, compute_volatility
from market.features.windows import TimeSeries
from market.health.state import BookHealth
from market.state.builder import build_market_state, compute_completeness
from market.state.quality import DataQuality, build_data_quality
from market.state.types import (
    UNAVAILABLE_TRADE_FEATURES,
    DepthFeatures,
    FlowFeatures,
    MarketIdentity,
    MarketState,
    PriceFeatures,
    ReturnsFeatures,
    StateTime,
    VolatilityFeatures,
)

#: 计算深度 feature 所需的最深档位数。
FEATURE_VIEW_DEPTH = max(DEPTH_LEVELS)


class FeatureEngine:
    """单一 symbol 的市场状态引擎。"""

    def __init__(
        self,
        venue: Venue,
        symbol: str,
        *,
        view_depth: int = FEATURE_VIEW_DEPTH,
        max_book_age_ms: Milliseconds | None = None,
    ) -> None:
        if view_depth < FEATURE_VIEW_DEPTH:
            raise ValueError(f"view_depth must be >= {FEATURE_VIEW_DEPTH}")
        self._identity = MarketIdentity(venue=venue, symbol=symbol)
        self._book = MarketBook(venue, symbol)
        self._view_depth = view_depth
        self._max_book_age_ms = max_book_age_ms
        self._history = TimeSeries(horizon_ms=PRICE_HISTORY_HORIZON_MS)
        self._flow = FlowFeaturesCalculator()
        # 成交域累积窗口 = 既有最大收益窗口（同一时间尺度，不另造窗口语义）
        self._trades = TradeFeatureAccumulator(window_ms=HISTORY_WINDOW_MS)
        self._last_state: MarketState | None = None
        self._event_ordinal = -1
        self._healthy = False
        self._sequence_contiguous = True
        self._last_book_update_exchange_ts: Milliseconds | None = None

    @property
    def venue(self) -> Venue:
        return self._identity.venue

    @property
    def symbol(self) -> str:
        return self._identity.symbol

    @property
    def book_health(self) -> BookHealth:
        """盘口可信度（只读转发；Owner 仍是本引擎内部的 `MarketBook`）。"""
        return self._book.health

    def on_market_event(self, event: MarketEvent) -> MarketState:
        """消费一条市场事件，返回该时刻的 immutable `MarketState`。

        P0001.17：TRADE 事件进入**成交域累积器**（`TradeFeatureAccumulator`），不喂给盘口；
        这样 `MarketState.trade`（vwap / cvd / trade_count / trade_intensity / 主动买卖量）是真实事实。
        """
        from market.events.payloads import TradePayload

        if isinstance(event.payload, TradePayload):
            return self._on_trade(event)

        update = self._book.on_market_event(event)
        self._event_ordinal += 1
        healthy = self._track_health(update, event)

        view = self._book.view(self._view_depth) if healthy else None
        price = compute_price_features(view)
        depth = compute_depth_features(view)
        if price.mid is not None:
            self._history.record(event.exchange_ts, price.mid)

        returns = compute_returns(self._history, at=event.exchange_ts, current_mid=price.mid)
        volatility = (
            compute_volatility(self._history, at=event.exchange_ts) if healthy else UNAVAILABLE_VOLATILITY
        )
        flow = self._flow.snapshot(
            at=event.exchange_ts,
            best_bid_size=price.bid_size,
            best_ask_size=price.ask_size,
        )
        quality = self._build_quality(
            event=event,
            price=price,
            depth=depth,
            flow=flow,
            returns=returns,
            volatility=volatility,
        )
        state = build_market_state(
            identity=self._identity,
            time=StateTime(
                as_of_exchange_ts=event.exchange_ts,
                as_of_receive_ts=event.receive_ts,
                event_ordinal=self._event_ordinal,
            ),
            quality=quality,
            price=price,
            depth=depth,
            flow=flow,
            # P0001.17：成交域来自真实 TRADE 事件累积（无成交 ⇒ None/UNAVAILABLE 语义保持）
            trade=self._trades.snapshot(at=event.exchange_ts),
            returns=returns,
            volatility=volatility,
        )
        self._last_state = state
        return state

    def _on_trade(self, event: MarketEvent) -> MarketState:
        """成交事件：只更新成交域累积，盘口/收益/波动率沿用上一个已知事实。"""
        self._trades.on_trade(event.payload, timestamp=event.exchange_ts)
        self._event_ordinal += 1
        previous = self._last_state
        trade = self._trades.snapshot(at=event.exchange_ts)
        if previous is None:
            # 首条事件就是成交：盘口事实仍 UNKNOWN（不伪造 mid/深度），但成交事实是真实的
            state = build_market_state(
                identity=self._identity,
                time=StateTime(as_of_exchange_ts=event.exchange_ts, as_of_receive_ts=event.receive_ts,
                               event_ordinal=self._event_ordinal),
                quality=self._build_quality(event=event, price=compute_price_features(None),
                                            depth=compute_depth_features(None),
                                            flow=self._flow.snapshot(at=event.exchange_ts, best_bid_size=None,
                                                                     best_ask_size=None),
                                            returns=compute_returns(self._history, at=event.exchange_ts,
                                                                    current_mid=None),
                                            volatility=UNAVAILABLE_VOLATILITY),
                price=compute_price_features(None), depth=compute_depth_features(None),
                flow=self._flow.snapshot(at=event.exchange_ts, best_bid_size=None, best_ask_size=None),
                trade=trade,
                returns=compute_returns(self._history, at=event.exchange_ts, current_mid=None),
                volatility=UNAVAILABLE_VOLATILITY)
            self._last_state = state
            return state
        state = build_market_state(
            identity=self._identity,
            time=StateTime(as_of_exchange_ts=event.exchange_ts, as_of_receive_ts=event.receive_ts,
                           event_ordinal=self._event_ordinal),
            quality=previous.quality, price=previous.price, depth=previous.depth, flow=previous.flow,
            trade=trade, returns=previous.returns, volatility=previous.volatility)
        self._last_state = state
        return state

    def request_resync(self) -> None:
        """在检测到 sequence gap 后请求重新同步（重订阅 / 重新拉取快照）。

        本引擎不实现传输层；该入口供调用方在观察到 `quality.book_health == STALE` 时使用。
        """
        self._book.request_resync()

    def invalidate(self, reason: str) -> None:
        """把盘口标记为不可信（P0001.9.1）：传输层丢失 / 重连时由调用方使用。

        与 `request_resync()` 配套：`invalidate()` 使盘口进入 `STALE`，随后调用
        `request_resync()` 进入 `RESYNCING`，直到新的快照 + 增量对齐后才重新 `HEALTHY`。
        """
        self._book.invalidate(reason)

    def _track_health(self, update: BookUpdate, event: MarketEvent) -> bool:
        """维护健康相关状态，返回本事件之后盘口是否 HEALTHY。"""
        healthy = self._book.health is BookHealth.HEALTHY
        if self._healthy and not healthy:
            # 盘口失健康：清空滚动 OFI 窗口，避免旧值被当成新数据。
            self._flow.invalidate_windows()
        self._healthy = healthy

        if update.health_after is BookHealth.STALE:
            self._sequence_contiguous = False
        if update.applied:
            self._last_book_update_exchange_ts = event.exchange_ts
            if healthy:
                self._flow.observe(update.mutations, timestamp=event.exchange_ts)
        return healthy

    def _build_quality(
        self,
        *,
        event: MarketEvent,
        price: PriceFeatures,
        depth: DepthFeatures,
        flow: FlowFeatures,
        returns: ReturnsFeatures,
        volatility: VolatilityFeatures,
    ) -> DataQuality:
        completeness = compute_completeness(
            price=price,
            depth=depth,
            flow=flow,
            returns=returns,
            volatility=volatility,
        )
        return build_data_quality(
            book_health=self._book.health,
            book_age_ms=self._book_age_ms(event.exchange_ts),
            sequence_contiguous=self._sequence_contiguous,
            window_coverage_ms=self._history.coverage_ms(event.exchange_ts),
            history_window_ms=HISTORY_WINDOW_MS,
            completeness=completeness,
            trade_stream_available=UNAVAILABLE_TRADE_FEATURES.trade_stream_available,
            # 本阶段没有成交流，因此不存在成交年龄。
            trade_age_ms=None,
            max_book_age_ms=self._max_book_age_ms,
        )

    def _book_age_ms(self, at: Milliseconds) -> Milliseconds | None:
        if self._last_book_update_exchange_ts is None:
            return None
        return max(0, at - self._last_book_update_exchange_ts)


__all__ = [
    "FEATURE_VIEW_DEPTH",
    "FeatureEngine",
]
