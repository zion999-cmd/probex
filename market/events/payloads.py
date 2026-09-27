"""市场事件载荷。

载荷是外部数据的只读投影：一经构造不再变化，可安全跨模块传递（CLAUDE.md §14）。
每个载荷在构造时即完成不变量校验，下游无需重复防御。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

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
    """深度增量：描述 `[first_update_id, last_update_id]` 区间内的档位变化。"""

    first_update_id: int
    last_update_id: int
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]

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
        object.__setattr__(self, "bids", _require_levels(self.bids, field="bids", allow_zero_size=True))
        object.__setattr__(self, "asks", _require_levels(self.asks, field="asks", allow_zero_size=True))
