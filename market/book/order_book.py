"""L2 盘口。

`OrderBook` 是档位状态（价格 -> 数量）与已应用序号的唯一 Owner（CLAUDE.md §14）：
外部只能通过 `apply_snapshot` / `apply_delta` 改变它，通过 `best_bid` / `best_ask` /
`depth` 只读访问。

本类不持有 BookHealth，也不做缓冲与恢复；那些属于 `MarketBook`。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from market.events.payloads import BookDeltaPayload, BookSnapshotPayload, PriceLevel
from market.events.types import Venue


class BookSide(Enum):
    """盘口方向。"""

    BID = "bid"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class BookMutation:
    """一次被应用的盘口档位变化。

    `OrderBook` 只负责「描述事实」：某个档位的 size 从 `old_size` 变为 `new_size`，
    以及这一次变化前后最优买卖档是什么。如何解释这些事实（例如 OFI）属于 Feature 层。

    `old_size == 0` 表示新增档位；`new_size == 0` 表示删除档位。
    """

    side: BookSide
    price: float
    old_size: float
    new_size: float
    best_bid_before: PriceLevel | None
    best_ask_before: PriceLevel | None
    best_bid_after: PriceLevel | None
    best_ask_after: PriceLevel | None


@dataclass(frozen=True, slots=True)
class DeltaApplication:
    """`apply_delta_with_mutations` 的结果。"""

    outcome: DeltaOutcome
    mutations: tuple[BookMutation, ...]


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
        #: 上一次同步是否来自快照（快照的 lastUpdateId 不是「上一条推送」，pu 在该刻不可比）
        self._anchored_by_snapshot = True
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
        self._anchored_by_snapshot = True
        self._invalidate_sorted_cache()

    def apply_delta(self, delta: BookDeltaPayload) -> DeltaOutcome:
        """应用增量，返回判定结果。仅在 `APPLIED` 时改动盘口。"""
        return self.apply_delta_with_mutations(delta).outcome

    def apply_delta_with_mutations(self, delta: BookDeltaPayload) -> DeltaApplication:
        """应用增量并逐档描述 mutation。仅在 `APPLIED` 时改动盘口。

        连续性判据（P0001.9.1.1 / D-028 / D-029）：

        - **快照锚点之后的第一条增量**用 vendor 文档的「跨过快照点」条件：
          快照序号 `L` 必须落在该增量的覆盖区间内（Futures：`pu <= L < u`；无 `pu`：`U <= L+1 <= u`）。
          因为 REST 快照的 `lastUpdateId` 不是「上一条推送」，不能与 `pu` 直接比较。
        - **之后**若增量携带 `previous_update_id`（Binance Futures `pu`），以 venue 语义为准：
          `previous_update_id == 已应用的最后 update id` 即连续 —— 一条事件内部跨越大量 update id
          是正常的聚合，不是丢失；只有 `pu` 对不上才是真实丢失（GAP → STALE → resync）。
        - 没有 `pu` 的增量（现货等）始终回退窗口规则（D-003）。
        """
        if self._last_update_id is None:
            return DeltaApplication(outcome=DeltaOutcome.AWAITING_SNAPSHOT, mutations=())
        if delta.last_update_id <= self._last_update_id:
            return DeltaApplication(outcome=DeltaOutcome.ALREADY_APPLIED, mutations=())
        if self._is_gap(delta):
            return DeltaApplication(outcome=DeltaOutcome.GAP, mutations=())

        mutations = [self._apply_level(BookSide.BID, level) for level in delta.bids]
        mutations += [self._apply_level(BookSide.ASK, level) for level in delta.asks]
        self._last_update_id = delta.last_update_id
        self._anchored_by_snapshot = False  # 之后的增量按 venue 的 pu 语义判连续
        return DeltaApplication(outcome=DeltaOutcome.APPLIED, mutations=tuple(mutations))

    def _is_gap(self, delta: BookDeltaPayload) -> bool:
        """该增量与已应用序号之间是否存在**真实**缺口。"""
        assert self._last_update_id is not None  # 调用点已保证
        if delta.previous_update_id is not None:
            if self._anchored_by_snapshot:
                # 锚点：`pu > L` 表示 (L, pu] 区间的推送从未被应用 ⇒ 真实缺口
                return delta.previous_update_id > self._last_update_id
            return delta.previous_update_id != self._last_update_id
        # 没有 pu 的 venue（现货）：文档的 update-id 窗口规则
        return delta.first_update_id > self._last_update_id + 1

    def reset(self) -> None:
        """清空盘口并回到未同步状态。"""
        self._bids = {}
        self._asks = {}
        self._last_update_id = None
        self._anchored_by_snapshot = True
        self._invalidate_sorted_cache()

    def best_bid(self) -> PriceLevel | None:
        return self._best_level(BookSide.BID)

    def best_ask(self) -> PriceLevel | None:
        return self._best_level(BookSide.ASK)

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

    def _apply_level(self, side: BookSide, level: PriceLevel) -> BookMutation:
        """应用单个档位变化并返回 mutation 描述。"""
        best_bid_before = self._best_level(BookSide.BID)
        best_ask_before = self._best_level(BookSide.ASK)
        levels = self._levels(side)
        old_size = levels.get(level.price, 0.0)
        self._set_level(levels, level)
        self._invalidate_sorted_cache()
        return BookMutation(
            side=side,
            price=level.price,
            old_size=old_size,
            new_size=level.size,
            best_bid_before=best_bid_before,
            best_ask_before=best_ask_before,
            best_bid_after=self._best_level(BookSide.BID),
            best_ask_after=self._best_level(BookSide.ASK),
        )

    def _best_level(self, side: BookSide) -> PriceLevel | None:
        levels = self._levels(side)
        if not levels:
            return None
        price = max(levels) if side is BookSide.BID else min(levels)
        return PriceLevel(price=price, size=levels[price])

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
