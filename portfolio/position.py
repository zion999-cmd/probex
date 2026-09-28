"""净持仓模型与成交应用逻辑（P0001.5 §2 / §3）。

第一版明确只支持：single symbol / net position / USDT-M 永续；不做 hedge mode。

方向语义：`qty > 0` long、`qty < 0` short、`qty == 0` flat。

反手（`long 1 @100` 后 `sell 2 @110`）必须拆成 **close leg + open leg**：
close 部分按旧均价实现盈亏，open 部分以成交价作为新均价，绝不混合出一个中间均价。

`realized_pnl` 只累计**交易盈亏**，不含手续费（手续费单独累计，见 `portfolio/accounting.py`）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from portfolio.types import Fill, InvalidFillError


@dataclass(frozen=True, slots=True)
class Position:
    """单一 symbol 的净持仓。"""

    symbol: str
    qty: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    mark_price: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise InvalidFillError("Position.symbol must be a non-empty string")
        for field in ("qty", "avg_entry_price", "realized_pnl"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise InvalidFillError(f"Position.{field} must be a finite number")
            object.__setattr__(self, field, float(value))
        if self.qty == 0.0 and self.avg_entry_price != 0.0:
            raise InvalidFillError("flat Position must have avg_entry_price == 0.0")
        if self.qty != 0.0 and self.avg_entry_price <= 0.0:
            raise InvalidFillError("open Position must have a positive avg_entry_price")
        if self.mark_price is not None:
            if isinstance(self.mark_price, bool) or not isinstance(self.mark_price, (int, float)):
                raise InvalidFillError("Position.mark_price must be a number or None")
            mark = float(self.mark_price)
            if not math.isfinite(mark) or mark <= 0.0:
                raise InvalidFillError(f"Position.mark_price must be positive and finite, got {self.mark_price!r}")
            object.__setattr__(self, "mark_price", mark)

    @property
    def is_flat(self) -> bool:
        return self.qty == 0.0

    @property
    def is_long(self) -> bool:
        return self.qty > 0.0

    @property
    def is_short(self) -> bool:
        return self.qty < 0.0

    @property
    def direction(self) -> str:
        if self.qty > 0.0:
            return "long"
        if self.qty < 0.0:
            return "short"
        return "flat"

    @property
    def notional(self) -> float | None:
        """按 mark price 计的名义价值；无 mark 时为 `None`（未知 ≠ 0）。"""
        if self.qty == 0.0:
            return 0.0
        if self.mark_price is None:
            return None
        return abs(self.qty) * self.mark_price

    @property
    def unrealized_pnl(self) -> float | None:
        """未实现盈亏：flat → 0.0；无 mark 且非 flat → `None`。"""
        if self.qty == 0.0:
            return 0.0
        if self.mark_price is None:
            return None
        return (self.mark_price - self.avg_entry_price) * self.qty

    def with_mark(self, mark_price: float) -> Position:
        """按新 mark price 重估（只影响 unrealized，不影响 realized）。"""
        return replace(self, mark_price=mark_price)


@dataclass(frozen=True, slots=True)
class PositionUpdate:
    """一次成交对持仓的影响（含拆分信息，便于审计）。"""

    position: Position
    realized_delta: float
    closed_qty: float
    opened_qty: float
    is_reversal: bool


def apply_fill(position: Position, fill: Fill) -> PositionUpdate:
    """把一笔成交应用到净持仓上。"""
    if position.symbol != fill.symbol:
        raise InvalidFillError(f"position {position.symbol!r} cannot consume fill for {fill.symbol!r}")

    delta = fill.signed_quantity
    qty = position.qty
    if qty == 0.0 or (qty > 0.0) == (delta > 0.0):
        return _open_or_add(position, fill, delta)
    return _reduce_or_reverse(position, fill, delta)


def _open_or_add(position: Position, fill: Fill, delta: float) -> PositionUpdate:
    """开仓或同向加仓：按绝对数量加权平均。"""
    new_qty = position.qty + delta
    new_avg = (abs(position.qty) * position.avg_entry_price + abs(delta) * fill.price) / abs(new_qty)
    return PositionUpdate(
        position=replace(position, qty=new_qty, avg_entry_price=new_avg),
        realized_delta=0.0,
        closed_qty=0.0,
        opened_qty=abs(delta),
        is_reversal=False,
    )


def _reduce_or_reverse(position: Position, fill: Fill, delta: float) -> PositionUpdate:
    """反向成交：先结算 close leg，再决定是纯平仓还是反手。"""
    closing_qty = min(abs(position.qty), abs(delta))
    direction = 1.0 if position.qty > 0.0 else -1.0
    realized_delta = (fill.price - position.avg_entry_price) * closing_qty * direction
    remaining = abs(delta) - closing_qty

    if remaining == 0.0:
        new_qty = position.qty + delta
        new_avg = position.avg_entry_price if new_qty != 0.0 else 0.0
        return PositionUpdate(
            position=replace(
                position,
                qty=new_qty,
                avg_entry_price=new_avg,
                realized_pnl=position.realized_pnl + realized_delta,
            ),
            realized_delta=realized_delta,
            closed_qty=closing_qty,
            opened_qty=0.0,
            is_reversal=False,
        )

    # 反手：open leg 以成交价作为新均价（不混合）
    return PositionUpdate(
        position=replace(
            position,
            qty=position.qty + delta,
            avg_entry_price=fill.price,
            realized_pnl=position.realized_pnl + realized_delta,
        ),
        realized_delta=realized_delta,
        closed_qty=closing_qty,
        opened_qty=remaining,
        is_reversal=True,
    )


__all__ = ["Position", "PositionUpdate", "apply_fill"]
