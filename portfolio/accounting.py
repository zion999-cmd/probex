"""Accounting Core：账户状态的唯一 Owner。

```text
Fill / Funding / Mark ──▶ AccountingCore ──▶ Position / Balance / Realized / Unrealized / Equity / Exposure
```

纪律（P0001.5）：

- `Fill` 是唯一成交事实源；Position / PnL / Balance 全部由账本派生。
- **Balance ≠ Equity**：`balance` 是已结算现金，`equity = balance + Σ unrealized`。
- **Realized ≠ Fee ≠ Funding**：`realized_pnl` 只含交易盈亏；费用累计到 `trading_fees`；
  资金费累计到 `funding`。`net_realized = realized_trade_pnl − trading_fees + funding`。
- **Mark price 只影响 unrealized / equity**，绝不改变 realized。
- **未知 ≠ 0**：非 flat 且无 mark 时，`unrealized_pnl` / `equity` 为 `None`（调用方必须 fail closed）。
- 一切时间戳来自事件或调用方注入，本模块不使用 wall-clock。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from market.events.types import Milliseconds
from portfolio.fills import FillLedger, FillOutcome
from portfolio.funding import FundingLedger
from portfolio.position import Position, apply_fill
from portfolio.types import (
    Fill,
    FundingPayment,
    UnsupportedAssetError,
)


@dataclass(frozen=True, slots=True)
class FillApplication:
    """一次 `record_fill` 的结果（重复成交也返回结果，但 realized/fee 均为 0）。"""

    outcome: FillOutcome
    fill: Fill
    position: Position
    realized_delta: float
    trading_fee_applied: float
    closed_qty: float
    opened_qty: float
    is_reversal: bool

    @property
    def accepted(self) -> bool:
        return self.outcome.accepted


class AccountingCore:
    """单一账户的账本与持仓状态。"""

    def __init__(self, *, settlement_asset: str = "USDT", initial_balance: float = 0.0) -> None:
        if not isinstance(settlement_asset, str) or not settlement_asset:
            raise ValueError("settlement_asset must be a non-empty string")
        if isinstance(initial_balance, bool) or not isinstance(initial_balance, (int, float)):
            raise ValueError("initial_balance must be a number")
        if not math.isfinite(float(initial_balance)):
            raise ValueError("initial_balance must be finite")

        self._settlement_asset = settlement_asset
        self._initial_balance = float(initial_balance)
        self._fills = FillLedger()
        self._funding = FundingLedger(settlement_asset=settlement_asset)
        self._positions: dict[str, Position] = {}
        self._marks: dict[str, float] = {}
        self._mark_timestamps: dict[str, Milliseconds] = {}
        self._realized_events: list[tuple[Milliseconds, float, float]] = []  # (ts, realized_delta, fee)
        self._peak_equity = float("-inf")

    # ------------------------------------------------------------------ 入账

    def record_fill(self, fill: Fill) -> FillApplication:
        """记录一笔成交并更新持仓。重复成交不改变任何状态。"""
        if fill.fee_asset != self._settlement_asset:
            raise UnsupportedAssetError(
                f"fill fee asset {fill.fee_asset!r} != settlement asset {self._settlement_asset!r}; "
                "多币种换算不在本阶段范围内"
            )

        outcome = self._fills.record(fill)
        position = self._positions.get(fill.symbol, Position(symbol=fill.symbol))
        if not outcome.accepted:
            return FillApplication(
                outcome=outcome,
                fill=fill,
                position=position,
                realized_delta=0.0,
                trading_fee_applied=0.0,
                closed_qty=0.0,
                opened_qty=0.0,
                is_reversal=False,
            )

        update = apply_fill(position, fill)
        marked = self._with_mark(update.position)
        self._positions[fill.symbol] = marked
        self._realized_events.append((fill.exchange_ts, update.realized_delta, fill.fee))
        self._refresh_peak_equity()

        return FillApplication(
            outcome=outcome,
            fill=fill,
            position=marked,
            realized_delta=update.realized_delta,
            trading_fee_applied=fill.fee,
            closed_qty=update.closed_qty,
            opened_qty=update.opened_qty,
            is_reversal=update.is_reversal,
        )

    def record_funding(self, payment: FundingPayment) -> None:
        """资金费独立入账（影响 balance / equity，不影响 realized trade pnl）。"""
        self._funding.record(payment)
        self._refresh_peak_equity()

    def update_mark_price(self, symbol: str, price: float, *, timestamp: Milliseconds) -> None:
        """显式注入 mark price（不使用 last trade）。"""
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a non-empty string")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise ValueError("price must be a number")
        mark = float(price)
        if not math.isfinite(mark) or mark <= 0.0:
            raise ValueError(f"mark price must be positive and finite, got {price!r}")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError("timestamp must be a non-negative int epoch-millisecond value")

        self._marks[symbol] = mark
        self._mark_timestamps[symbol] = timestamp
        position = self._positions.get(symbol)
        if position is not None:
            self._positions[symbol] = position.with_mark(mark)
        self._refresh_peak_equity()

    # ------------------------------------------------------------------ 只读事实

    @property
    def settlement_asset(self) -> str:
        return self._settlement_asset

    @property
    def fills(self) -> FillLedger:
        return self._fills

    @property
    def funding(self) -> FundingLedger:
        return self._funding

    def position(self, symbol: str) -> Position:
        return self._positions.get(symbol, Position(symbol=symbol))

    @property
    def positions(self) -> tuple[Position, ...]:
        return tuple(self._positions.values())

    def mark_price(self, symbol: str) -> float | None:
        return self._marks.get(symbol)

    def mark_timestamp(self, symbol: str) -> Milliseconds | None:
        return self._mark_timestamps.get(symbol)

    @property
    def initial_balance(self) -> float:
        return self._initial_balance

    @property
    def realized_trade_pnl(self) -> float:
        """累计已实现**交易**盈亏（不含手续费、不含资金费）。"""
        return float(sum(position.realized_pnl for position in self._positions.values()))

    @property
    def trading_fees(self) -> float:
        """累计交易手续费（独立于 realized / funding）。"""
        return float(sum(fee for _ts, _delta, fee in self._realized_events))

    @property
    def funding_total(self) -> float:
        return self._funding.total

    @property
    def net_realized(self) -> float:
        """`realized_trade_pnl − trading_fees + funding`。"""
        return self.realized_trade_pnl - self.trading_fees + self.funding_total

    @property
    def balance(self) -> float:
        """已结算现金（Balance ≠ Equity）。"""
        return self._initial_balance + self.net_realized

    def unrealized_pnl(self) -> float | None:
        """Σ 未实现盈亏；任一非 flat 持仓缺 mark 时为 `None`。"""
        total = 0.0
        for position in self._positions.values():
            value = position.unrealized_pnl
            if value is None:
                return None
            total += value
        return float(total)

    def equity(self) -> float | None:
        """`balance + Σ unrealized`；未实现未知时为 `None`。"""
        unrealized = self.unrealized_pnl()
        if unrealized is None:
            return None
        return self.balance + unrealized

    @property
    def peak_equity(self) -> float | None:
        """历史最高 equity（用于 drawdown）；从未观测到 equity 时为 `None`。"""
        return None if self._peak_equity == float("-inf") else self._peak_equity

    def drawdown(self) -> float | None:
        """`peak_equity − equity`（>= 0）；数据不足时为 `None`。"""
        equity = self.equity()
        peak = self.peak_equity
        if equity is None or peak is None:
            return None
        return max(0.0, peak - equity)

    @property
    def gross_exposure(self) -> float | None:
        """Σ|notional|；任一非 flat 持仓缺 mark 时为 `None`。"""
        total = 0.0
        for position in self._positions.values():
            notional = position.notional
            if notional is None:
                return None
            total += notional
        return float(total)

    @property
    def net_exposure(self) -> float | None:
        """|Σ signed notional|；任一非 flat 持仓缺 mark 时为 `None`。"""
        total = 0.0
        for position in self._positions.values():
            notional = position.notional
            if notional is None:
                return None
            total += notional if position.qty > 0.0 else -notional
        return abs(float(total))

    def realized_trade_pnl_since(self, timestamp: Milliseconds) -> float:
        return float(sum(delta for ts, delta, _fee in self._realized_events if ts >= timestamp))

    def trading_fees_since(self, timestamp: Milliseconds) -> float:
        return float(sum(fee for ts, _delta, fee in self._realized_events if ts >= timestamp))

    def funding_since(self, timestamp: Milliseconds) -> float:
        return self._funding.total_since(timestamp)

    def net_realized_since(self, timestamp: Milliseconds) -> float:
        """`realized − fees + funding`（用于 Daily Loss 判定）。"""
        return (
            self.realized_trade_pnl_since(timestamp)
            - self.trading_fees_since(timestamp)
            + self.funding_since(timestamp)
        )

    # ------------------------------------------------------------------ 内部

    def _with_mark(self, position: Position) -> Position:
        mark = self._marks.get(position.symbol)
        return position if mark is None else position.with_mark(mark)

    def _refresh_peak_equity(self) -> None:
        equity = self.equity()
        if equity is None:
            return
        if equity > self._peak_equity:
            self._peak_equity = equity


__all__ = ["AccountingCore", "FillApplication"]
