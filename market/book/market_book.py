"""MarketBook：L2 盘口 + BookHealth 的协同入口。

`MarketBook` 是「某个 symbol 的盘口是否可信」这一状态的唯一 Owner（CLAUDE.md §14）：

- `OrderBook` 拥有档位与已应用序号；
- `MarketBook` 拥有 `BookHealth`、重同步缓冲与状态转换记录。

事件按到达顺序串行处理（同一 `MarketBook` 实例不得并发投喂），因此缓冲重放与
状态转换不会与增量事件交错。

同步闭环（P0001.1 §1.2）：

```text
AWAITING_SNAPSHOT --snapshot--> RESYNCING --replay buffer--> HEALTHY
HEALTHY --delta gap--> STALE --request_resync--> RESYNCING --snapshot--> ...
```

只要 `is_tradeable` 为 `False`，下游不得输出交易可用状态。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from market.book.errors import MarketBookInvariantError, UnexpectedMarketEventError
from market.book.order_book import BookMutation, BookSide, DeltaOutcome, OrderBook
from market.events.payloads import BookDeltaPayload, BookSnapshotPayload, PriceLevel
from market.events.types import EventType, MarketEvent, Venue
from market.health.state import BookHealth, HealthTransition, IllegalHealthTransition, require_transition

#: 未同步期间允许缓冲的增量上限；超出后丢弃最旧增量（新快照只会用到较新的增量）。
DEFAULT_RESYNC_BUFFER_LIMIT = 1024

#: `view()` 默认返回的档位数。
DEFAULT_VIEW_DEPTH = 10


@dataclass(frozen=True, slots=True)
class BookUpdate:
    """一次事件处理的结果。

    `mutations` 是本次实际应用的档位变化（P0001.3 起提供，供 Feature 层解释）；
    未被应用（缓冲 / 重复 / gap）时为空元组。
    """

    event_type: EventType
    sequence: int
    health_before: BookHealth
    health_after: BookHealth
    outcome: DeltaOutcome | None
    applied: bool
    buffered_deltas: int
    #: 为 True 表示调用方需要重新订阅 / 重新拉取快照以完成恢复。
    resync_required: bool
    mutations: tuple[BookMutation, ...] = ()


@dataclass(frozen=True, slots=True)
class BookView:
    """盘口只读快照，携带可信度。

    下游（Feature / Jev / Strategy）只应消费 `BookView`，不得直接持有 `OrderBook`。
    """

    venue: Venue
    symbol: str
    health: BookHealth
    is_tradeable: bool
    last_update_id: int | None
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]


class MarketBook:
    """单一 symbol 的盘口与可信度状态机。"""

    def __init__(
        self,
        venue: Venue,
        symbol: str,
        *,
        resync_buffer_limit: int = DEFAULT_RESYNC_BUFFER_LIMIT,
    ) -> None:
        if resync_buffer_limit <= 0:
            raise ValueError("resync_buffer_limit must be > 0")
        self._venue = venue
        self._symbol = symbol
        self._book = OrderBook(venue, symbol)
        self._health = BookHealth.AWAITING_SNAPSHOT
        self._buffer: deque[BookDeltaPayload] = deque(maxlen=resync_buffer_limit)
        self._last_transition: HealthTransition | None = None

    @property
    def venue(self) -> Venue:
        return self._venue

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def health(self) -> BookHealth:
        return self._health

    @property
    def is_tradeable(self) -> bool:
        """可交易门：只有 `HEALTHY` 盘口才允许下游输出交易可用状态。"""
        return self._health is BookHealth.HEALTHY

    @property
    def last_update_id(self) -> int | None:
        return self._book.last_update_id

    @property
    def last_transition(self) -> HealthTransition | None:
        return self._last_transition

    def best_bid(self) -> PriceLevel | None:
        return self._book.best_bid()

    def best_ask(self) -> PriceLevel | None:
        return self._book.best_ask()

    def depth(self, side: BookSide, limit: int | None = None) -> tuple[PriceLevel, ...]:
        return self._book.depth(side, limit)

    def view(self, depth: int = DEFAULT_VIEW_DEPTH) -> BookView:
        """返回盘口只读快照。未同步时档位为空。"""
        return BookView(
            venue=self._venue,
            symbol=self._symbol,
            health=self._health,
            is_tradeable=self.is_tradeable,
            last_update_id=self._book.last_update_id,
            bids=self._book.depth(BookSide.BID, depth),
            asks=self._book.depth(BookSide.ASK, depth),
        )

    def on_market_event(self, event: MarketEvent) -> BookUpdate:
        """处理一条市场事件。非本盘口的事件与不受支持的事件类型直接拒绝。"""
        if event.venue is not self._venue or event.symbol != self._symbol:
            raise UnexpectedMarketEventError(
                f"MarketBook({self._venue.value}/{self._symbol}) received event for "
                f"{event.venue.value}/{event.symbol}"
            )
        if isinstance(event.payload, BookSnapshotPayload):
            return self._on_snapshot(event, event.payload)
        if isinstance(event.payload, BookDeltaPayload):
            return self._on_delta(event, event.payload)
        raise UnexpectedMarketEventError(f"unsupported event type: {event.event_type.value}")

    def invalidate(self, reason: str) -> None:
        """标记盘口不可信（P0001.9.1）：由传输层在丢失连接 / 重连时调用。

        传输层丢失时不可能伪造一条 gap 事件，但下游必须立刻停止把盘口当作可信：
        `HEALTHY -> STALE` 是既有合法转换，这里只新增**入口**，不改变任何转换语义。
        非 `HEALTHY` 时是幂等 no-op（`AWAITING_SNAPSHOT / RESYNCING` 不允许直接进入 `STALE`）。
        """
        if not isinstance(reason, str) or not reason:
            raise MarketBookInvariantError("invalidate reason must be a non-empty string")
        if self._health is not BookHealth.HEALTHY:
            return
        self._enter_stale(reason=reason)

    def request_resync(self) -> None:
        """在检测到 gap 后由传输层调用（重订阅 / 重新拉取快照）。

        只允许从 `STALE` 进入；`RESYNCING` 下重复调用是幂等的。
        """
        if self._health is BookHealth.RESYNCING:
            return
        if self._health is not BookHealth.STALE:
            raise IllegalHealthTransition(
                f"resync can only be requested from {BookHealth.STALE.value}, current health is {self._health.value}"
            )
        self._transition(BookHealth.RESYNCING, reason="resync_requested")

    def _on_delta(self, event: MarketEvent, delta: BookDeltaPayload) -> BookUpdate:
        health_before = self._health
        if health_before is not BookHealth.HEALTHY:
            self._buffer.append(delta)
            return BookUpdate(
                event_type=event.event_type,
                sequence=event.payload.last_update_id,
                health_before=health_before,
                health_after=self._health,
                outcome=None,
                applied=False,
                buffered_deltas=len(self._buffer),
                resync_required=False,
            )

        outcome = self._book.apply_delta_with_mutations(delta)
        if outcome.outcome is DeltaOutcome.APPLIED:
            return self._update(
                event,
                health_before,
                outcome.outcome,
                applied=True,
                resync_required=False,
                mutations=outcome.mutations,
            )
        if outcome.outcome is DeltaOutcome.ALREADY_APPLIED:
            return self._update(event, health_before, outcome.outcome, applied=False, resync_required=False)
        if outcome.outcome is DeltaOutcome.GAP:
            self._enter_stale(reason="sequence_gap")
            return self._update(event, health_before, outcome.outcome, applied=False, resync_required=True)
        raise MarketBookInvariantError(
            f"HEALTHY book reported {outcome.outcome.value} for delta {delta.first_update_id}-{delta.last_update_id}"
        )

    def _on_snapshot(self, event: MarketEvent, snapshot: BookSnapshotPayload) -> BookUpdate:
        health_before = self._health
        self._book.apply_snapshot(snapshot)
        self._transition(BookHealth.RESYNCING, reason="snapshot_applied")

        if self._replay_buffer():
            self._enter_healthy(reason="resync_complete")
            resync_required = False
        else:
            self._enter_stale(reason="replay_sequence_gap")
            resync_required = True

        return BookUpdate(
            event_type=event.event_type,
            sequence=snapshot.last_update_id,
            health_before=health_before,
            health_after=self._health,
            outcome=None,
            applied=True,
            buffered_deltas=len(self._buffer),
            resync_required=resync_required,
        )

    def _replay_buffer(self) -> bool:
        """按顺序重放缓冲增量。返回 False 表示重放中再次发现 gap。"""
        while self._buffer:
            delta = self._buffer[0]
            outcome = self._book.apply_delta(delta)
            if outcome is DeltaOutcome.APPLIED or outcome is DeltaOutcome.ALREADY_APPLIED:
                self._buffer.popleft()
                continue
            if outcome is DeltaOutcome.GAP:
                return False
            raise MarketBookInvariantError(f"unexpected outcome while replaying buffer: {outcome.value}")
        return True

    def _update(
        self,
        event: MarketEvent,
        health_before: BookHealth,
        outcome: DeltaOutcome,
        *,
        applied: bool,
        resync_required: bool,
        mutations: tuple[BookMutation, ...] = (),
    ) -> BookUpdate:
        return BookUpdate(
            event_type=event.event_type,
            sequence=event.payload.last_update_id,
            health_before=health_before,
            health_after=self._health,
            outcome=outcome,
            applied=applied,
            buffered_deltas=len(self._buffer),
            resync_required=resync_required,
            mutations=mutations,
        )

    def _enter_stale(self, *, reason: str) -> None:
        self._transition(BookHealth.STALE, reason=reason)
        # gap 之前的缓冲增量已不可能接上当前序号，全部丢弃，只保留 gap 之后的增量。
        self._buffer.clear()

    def _enter_healthy(self, *, reason: str) -> None:
        self._transition(BookHealth.HEALTHY, reason=reason)
        self._buffer.clear()

    def _transition(self, target: BookHealth, *, reason: str) -> None:
        if target is self._health:
            return
        require_transition(self._health, target)
        self._last_transition = HealthTransition(from_health=self._health, to_health=target, reason=reason)
        self._health = target
