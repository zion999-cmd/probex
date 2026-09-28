"""Fill Ledger：成交事实的唯一来源 + 去重。

规则（P0001.5 §1）：

- append-only：只追加已接受的 Fill，不提供修改/删除入口。
- 去重：`(venue, symbol, fill_id)` 与 `(venue, symbol, trade_id)` 双键，
  任一键重复即视为重复成交 → 只返回 outcome，**不重复记账**。
- 重复不是异常（重放 / 补推 / 重试都会发生），但必须可被计数与审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from portfolio.types import Fill


class FillOutcome(Enum):
    """一次 `record` 的结果。"""

    RECORDED = "recorded"
    DUPLICATE_FILL_ID = "duplicate_fill_id"
    DUPLICATE_TRADE_ID = "duplicate_trade_id"

    @property
    def accepted(self) -> bool:
        return self is FillOutcome.RECORDED

    @property
    def is_duplicate(self) -> bool:
        return not self.accepted


@dataclass
class FillLedger:
    """append-only 成交账本。"""

    _fills: list[Fill] = field(default_factory=list)
    _fill_keys: set[tuple[str, str, str]] = field(default_factory=set)
    _trade_keys: set[tuple[str, str, str]] = field(default_factory=set)
    _duplicates: int = 0

    def record(self, fill: Fill) -> FillOutcome:
        """记录一笔成交；重复成交只返回 outcome，不改变账本。"""
        fill_key = (fill.venue.value, fill.symbol, fill.fill_id)
        if fill_key in self._fill_keys:
            self._duplicates += 1
            return FillOutcome.DUPLICATE_FILL_ID
        trade_key = (fill.venue.value, fill.symbol, fill.trade_id)
        if trade_key in self._trade_keys:
            self._duplicates += 1
            return FillOutcome.DUPLICATE_TRADE_ID

        self._fill_keys.add(fill_key)
        self._trade_keys.add(trade_key)
        self._fills.append(fill)
        return FillOutcome.RECORDED

    def has(self, fill: Fill) -> bool:
        """该成交是否已经记账过（按双键判定）。"""
        return (fill.venue.value, fill.symbol, fill.fill_id) in self._fill_keys or (
            fill.venue.value,
            fill.symbol,
            fill.trade_id,
        ) in self._trade_keys

    @property
    def fills(self) -> tuple[Fill, ...]:
        return tuple(self._fills)

    @property
    def count(self) -> int:
        return len(self._fills)

    @property
    def duplicate_count(self) -> int:
        return self._duplicates

    def for_symbol(self, symbol: str) -> tuple[Fill, ...]:
        return tuple(fill for fill in self._fills if fill.symbol == symbol)

    def __len__(self) -> int:
        return len(self._fills)


__all__ = ["FillLedger", "FillOutcome"]
