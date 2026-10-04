"""市场事件载荷。

载荷是外部数据的只读投影：一经构造不再变化，可安全跨模块传递（CLAUDE.md §14）。
每个载荷在构造时即完成不变量校验，下游无需重复防御。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from market.events.errors import InvalidPayloadError


def _require_finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidPayloadError(f"{field} must be a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise InvalidPayloadError(f"{field} must be finite, got {value!r}")
    return number


@dataclass(frozen=True, slots=True)
class PriceLevel:
    """单一价格档位。

    `size == 0` 在 `BookDeltaPayload` 中表示删除该档位；在 `BookSnapshotPayload` 中非法。
    """

    price: float
    size: float

    def __post_init__(self) -> None:
        price = _require_finite_number(self.price, field="PriceLevel.price")
        size = _require_finite_number(self.size, field="PriceLevel.size")
        if price <= 0.0:
            raise InvalidPayloadError(f"PriceLevel.price must be positive, got {price!r}")
        if size < 0.0:
            raise InvalidPayloadError(f"PriceLevel.size must be non-negative, got {size!r}")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "size", size)


def _require_levels(value: object, *, field: str, allow_zero_size: bool) -> tuple[PriceLevel, ...]:
    if not isinstance(value, (tuple, list)):
        raise InvalidPayloadError(f"{field} must be a sequence of levels")
    levels: list[PriceLevel] = []
    seen: set[float] = set()
    for index, item in enumerate(value):
        if not isinstance(item, PriceLevel):
            raise InvalidPayloadError(f"{field}[{index}] must be a PriceLevel, got {type(item).__name__}")
        if not allow_zero_size and item.size == 0.0:
            raise InvalidPayloadError(f"{field}[{index}] size must be positive in a snapshot")
        if item.price in seen:
            raise InvalidPayloadError(f"{field} contains duplicate price {item.price!r}")
        seen.add(item.price)
        levels.append(item)
    return tuple(levels)


@dataclass(frozen=True, slots=True)
class BookSnapshotPayload:
    """深度快照：完整重述某一时刻的盘口。"""

    last_update_id: int
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]

    def __post_init__(self) -> None:
        if isinstance(self.last_update_id, bool) or not isinstance(self.last_update_id, int):
            raise InvalidPayloadError("BookSnapshotPayload.last_update_id must be an int")
        if self.last_update_id < 0:
            raise InvalidPayloadError(f"BookSnapshotPayload.last_update_id must be >= 0, got {self.last_update_id}")
        object.__setattr__(self, "bids", _require_levels(self.bids, field="bids", allow_zero_size=False))
        object.__setattr__(self, "asks", _require_levels(self.asks, field="asks", allow_zero_size=False))


@dataclass(frozen=True, slots=True)
class BookDeltaPayload:
    """深度增量：描述 `[first_update_id, last_update_id]` 区间内的档位变化。

    `previous_update_id`（Binance Futures 的 `pu`）是**上一条推送的最终 update id**：
    Futures 的 diff 事件会聚合成千上万个 update id，因此连续性的正确判据是
    `previous_update_id == 上一条的 last_update_id`，而不是 `first_update_id == 上一条 last + 1`
    （见 P0001.9.1.1 / D-028）。无该字段的 venue / stream 保持 `None`，由下游回退到窗口规则。
    """

    first_update_id: int
    last_update_id: int
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    #: 上一条推送的最终 update id（`pu`）；不适用时为 None。
    previous_update_id: int | None = None

    def __post_init__(self) -> None:
        for name in ("first_update_id", "last_update_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidPayloadError(f"BookDeltaPayload.{name} must be an int")
            if value < 0:
                raise InvalidPayloadError(f"BookDeltaPayload.{name} must be >= 0, got {value}")
        if self.last_update_id < self.first_update_id:
            raise InvalidPayloadError(
                f"BookDeltaPayload.last_update_id ({self.last_update_id}) < first_update_id ({self.first_update_id})"
            )
        if self.previous_update_id is not None:
            value = self.previous_update_id
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidPayloadError("BookDeltaPayload.previous_update_id must be an int or None")
            if value < 0:
                raise InvalidPayloadError(
                    f"BookDeltaPayload.previous_update_id must be >= 0, got {value}"
                )
        object.__setattr__(self, "bids", _require_levels(self.bids, field="bids", allow_zero_size=True))
        object.__setattr__(self, "asks", _require_levels(self.asks, field="asks", allow_zero_size=True))


class AggressorSide(Enum):
    """主动方方向（taker 侧）。

    定义在 market 层：`portfolio.Side` 依赖 market（`portfolio.types` 已 import 本模块），
    因此 market **不能**反向依赖 portfolio（CLAUDE.md §14 单一 Owner / 无环依赖）。
    """

    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> AggressorSide:
        return AggressorSide.SELL if self is AggressorSide.BUY else AggressorSide.BUY


@dataclass(frozen=True, slots=True)
class TradePayload:
    """聚合成交（aggressor trade）事件载荷。

    P0001.8 的最小成交证据：**只有**这种事件可以推进 Maker 队列或产生成交
    （L2 quantity 下降不算成交证据，见 proposals/P0001.8 §4）。

    字段：

    - `aggregate_trade_id`：交易所聚合成交 id（同 symbol 内唯一、单调），用于**去重**；
    - `price` / `quantity`：该次聚合成交的价格与数量；
    - `aggressor`：主动方（吃单方）方向 —— 与我们的挂单方向相反时才消耗队列。
    """

    aggregate_trade_id: int
    price: float
    quantity: float
    aggressor: AggressorSide

    def __post_init__(self) -> None:
        if isinstance(self.aggregate_trade_id, bool) or not isinstance(self.aggregate_trade_id, int):
            raise InvalidPayloadError("TradePayload.aggregate_trade_id must be an int")
        if self.aggregate_trade_id < 0:
            raise InvalidPayloadError(
                f"TradePayload.aggregate_trade_id must be >= 0, got {self.aggregate_trade_id}"
            )
        price = _require_finite_number(self.price, field="TradePayload.price")
        quantity = _require_finite_number(self.quantity, field="TradePayload.quantity")
        if price <= 0.0:
            raise InvalidPayloadError(f"TradePayload.price must be positive, got {price!r}")
        if quantity <= 0.0:
            raise InvalidPayloadError(f"TradePayload.quantity must be positive, got {quantity!r}")
        if not isinstance(self.aggressor, AggressorSide):
            raise InvalidPayloadError(
                f"TradePayload.aggressor must be an AggressorSide, got {type(self.aggressor).__name__}"
            )
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "quantity", quantity)

@dataclass(frozen=True, slots=True)
class MarkPricePayload:
    """正式 mark price 事件载荷（P0001.15 §11–§12 + 人类裁决 1A）。

    `MARK` 是**独立的价格语义**：它由 venue 正式发布（Binance `markPriceUpdate`），
    **不是** last trade。本阶段不提供 `INDEX` 来源（只有 vocabulaory 允许表达）。
    """

    price: float

    def __post_init__(self) -> None:
        price = _require_finite_number(self.price, field="MarkPricePayload.price")
        if price <= 0.0:
            raise InvalidPayloadError(f"MarkPricePayload.price must be positive, got {price!r}")
        object.__setattr__(self, "price", price)

