"""Funding 账本：资金费独立入账。

资金费既不是 Fill，也不是 Fee，也不是已实现交易盈亏；但它影响 Balance / Equity / Net PnL
（P0001.5 §6）。正数表示入账，负数表示支出。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from market.events.types import Milliseconds
from portfolio.types import FundingPayment, UnsupportedAssetError


@dataclass
class FundingLedger:
    """append-only 资金费账本。"""

    settlement_asset: str = "USDT"
    _payments: list[FundingPayment] = field(default_factory=list)

    def record(self, payment: FundingPayment) -> None:
        if payment.asset != self.settlement_asset:
            raise UnsupportedAssetError(
                f"funding asset {payment.asset!r} != settlement asset {self.settlement_asset!r}; "
                "多币种换算不在本阶段范围内"
            )
        self._payments.append(payment)

    @property
    def payments(self) -> tuple[FundingPayment, ...]:
        return tuple(self._payments)

    @property
    def total(self) -> float:
        """累计资金费（正数=净入账）。"""
        return float(sum(payment.amount for payment in self._payments))

    def for_symbol(self, symbol: str) -> tuple[FundingPayment, ...]:
        return tuple(payment for payment in self._payments if payment.symbol == symbol)

    def total_since(self, timestamp: Milliseconds) -> float:
        """`timestamp`（含）之后的资金费合计。"""
        return float(sum(payment.amount for payment in self._payments if payment.timestamp >= timestamp))

    def __len__(self) -> int:
        return len(self._payments)


__all__ = ["FundingLedger"]
