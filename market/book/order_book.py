"""L2 盘口。

`OrderBook` 是档位状态（价格 -> 数量）与已应用序号的唯一 Owner（CLAUDE.md §14）：
外部只能通过 `apply_snapshot` / `apply_delta` 改变它，通过 `best_bid` / `best_ask` /
`depth` 只读访问。

本类不持有 BookHealth，也不做缓冲与恢复；那些属于 `MarketBook`。
"""

from __future__ import annotations

from enum import Enum

from market.events.payloads import BookDeltaPayload, BookSnapshotPayload, PriceLevel
from market.events.types import Venue


class BookSide(Enum):
    """盘口方向。"""

    BID = "bid"
    ASK = "ask"


class DeltaOutcome(Enum):
    """`apply_delta` 对增量事件的判定结果。"""

    #: 增量已应用，盘口前移到 `last_update_id`。
    APPLIED = "applied"
    #: 增量已被覆盖（`last_update_id <= 当前序号`），幂等忽略。
    ALREADY_APPLIED = "already_applied"
    #: 检测到 sequence gap（`first_update_id > 当前序号 + 1`），盘口未改动。
    GAP = "gap"
    #: 尚未应用任何快照，无基线可用，盘口未改动。
    AWAITING_SNAPSHOT = "awaiting_snapshot"


class OrderBook:
    """单一 symbol 的 L2 盘口。"""

    def __init__(self, venue: Venue, symbol: str) -> None:
        if not symbol:
            raise ValueError("symbol must be a non-empty string")
        self._venue = venue
        self._symbol = symbol
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._last_update_id: int | None = None
        self._sorted_bids: tuple[float, ...] | None = None
        self._sorted_asks: tuple[float, ...] | None = None

    @property
    def venue(self) -> Venue:
        return self._venue

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def last_update_id(self) -> int | None:
        """最后一个已应用更新的交易所序号；未同步时为 `None`。"""
        return self._last_update_id

    @property
    def is_initialized(self) -> bool:
        """是否已应用过快照。"""
        return self._last_update_id is not None

    def apply_snapshot(self, snapshot: BookSnapshotPayload) -> None:
        """用快照整体替换盘口，并把序号基线设为快照的 `last_update_id`。"""
        self._bids = {level.price: level.size for level in snapshot.bids}
        self._asks = {level.price: level.size for level in snapshot.asks}
        self._last_update_id = snapshot.last_update_id
        self._invalidate_sorted_cache()

    def apply_delta(self, delta: BookDeltaPayload) -> DeltaOutcome:
        """应用增量，返回判定结果。仅在 `APPLIED` 时改动盘口。"""
        if self._last_update_id is None:
            return DeltaOutcome.AWAITING_SNAPSHOT
        if delta.last_update_id <= self._last_update_id:
            return DeltaOutcome.ALREADY_APPLIED
        if delta.first_update_id > self._last_update_id + 1:
            return DeltaOutcome.GAP

        for level in delta.bids:
            self._set_level(self._bids, level)
        for level in delta.asks:
            self._set_level(self._asks, level)
        self._last_update_id = delta.last_update_id
        self._invalidate_sorted_cache()
        return DeltaOutcome.APPLIED

    def reset(self) -> None:
        """清空盘口并回到未同步状态。"""
        self._bids = {}
        self._asks = {}
        self._last_update_id = None
        self._invalidate_sorted_cache()

    def best_bid(self) -> PriceLevel | None:
        prices = self._sorted_prices(BookSide.BID)
        if not prices:
            return None
        price = prices[0]
        return PriceLevel(price=price, size=self._bids[price])

    def best_ask(self) -> PriceLevel | None:
        prices = self._sorted_prices(BookSide.ASK)
        if not prices:
            return None
        price = prices[0]
        return PriceLevel(price=price, size=self._asks[price])

    def depth(self, side: BookSide, limit: int | None = None) -> tuple[PriceLevel, ...]:
        """按撮合优先级返回档位：bid 价格从高到低，ask 价格从低到高。"""
        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0 or None")
        levels = self._bids if side is BookSide.BID else self._asks
        prices = self._sorted_prices(side)
        if limit is not None:
            prices = prices[:limit]
        return tuple(PriceLevel(price=price, size=levels[price]) for price in prices)

    def level_count(self, side: BookSide) -> int:
        return len(self._bids if side is BookSide.BID else self._asks)

    @staticmethod
    def _set_level(levels: dict[float, float], level: PriceLevel) -> None:
        """`size == 0` 表示删除档位。"""
        if level.size == 0.0:
            levels.pop(level.price, None)
        else:
            levels[level.price] = level.size

    def _levels(self, side: BookSide) -> dict[float, float]:
        return self._bids if side is BookSide.BID else self._asks

    def _sorted_prices(self, side: BookSide) -> tuple[float, ...]:
        cached = self._sorted_bids if side is BookSide.BID else self._sorted_asks
        if cached is None:
            cached = tuple(sorted(self._levels(side), reverse=side is BookSide.BID))
            if side is BookSide.BID:
                self._sorted_bids = cached
            else:
                self._sorted_asks = cached
        return cached

    def _invalidate_sorted_cache(self) -> None:
        self._sorted_bids = None
        self._sorted_asks = None
