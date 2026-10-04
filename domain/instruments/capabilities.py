"""Instrument capabilities（P0001.15 §4）：描述产品**允许什么交易能力**。

这些是 **instrument / product semantics**，不是 venue rules，也**不得**由 Strategy 依据 venue 名称推断
（人类裁决 3：venue 名称不得决定 product semantics）。

P0001.15 只有 CRYPTO / PERPETUAL 具有生产实现；EQUITY / FUTURE 只作为 domain vocabulary 存在。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TradingSessionType(Enum):
    """交易时段语义（EQUITY 用 `REGULAR_HOURS`；Crypto 为 `CONTINUOUS`）。"""

    CONTINUOUS = "CONTINUOUS"
    REGULAR_HOURS = "REGULAR_HOURS"


class SettlementType(Enum):
    """结算语义。"""

    PERPETUAL_FUNDING = "PERPETUAL_FUNDING"
    CASH = "CASH"
    PHYSICAL_DELIVERY = "PHYSICAL_DELIVERY"


@dataclass(frozen=True, slots=True)
class InstrumentCapabilities:
    """某个 instrument 的交易能力（只读事实，不参与风险数学 —— 人类裁决 3）。"""

    supports_long: bool
    supports_short: bool
    supports_margin: bool
    supports_reduce_only: bool
    has_expiry: bool
    has_funding: bool
    has_corporate_actions: bool
    supports_market_order: bool
    supports_limit_order: bool
    supports_post_only: bool
    trading_session_type: TradingSessionType
    settlement_type: SettlementType

    def __post_init__(self) -> None:
        for name in ("supports_long", "supports_short", "supports_margin", "supports_reduce_only",
                     "has_expiry", "has_funding", "has_corporate_actions", "supports_market_order",
                     "supports_limit_order", "supports_post_only"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise ValueError(f"InstrumentCapabilities.{name} must be a bool")
        if not isinstance(self.trading_session_type, TradingSessionType):
            raise ValueError("InstrumentCapabilities.trading_session_type must be a TradingSessionType")
        if not isinstance(self.settlement_type, SettlementType):
            raise ValueError("InstrumentCapabilities.settlement_type must be a SettlementType")
        # 结构性不变量（定义性事实，不含业务数值）：
        if not (self.supports_long or self.supports_short):
            raise ValueError("an instrument must support at least one direction")
        if self.supports_reduce_only and not (self.supports_long or self.supports_short):
            raise ValueError("supports_reduce_only requires a position-holding direction")
        if self.supports_post_only and not self.supports_limit_order:
            raise ValueError("supports_post_only requires supports_limit_order")
        if self.supports_market_order and not self.supports_limit_order and not self.supports_market_order:
            raise ValueError("unreachable guard")  # pragma: no cover - 保留结构对称性

    @property
    def supports_both_directions(self) -> bool:
        return self.supports_long and self.supports_short

    def view(self) -> dict[str, object]:
        """审计 / UI 用的扁平视图（不含任何推断）。"""
        return {
            "supports_long": self.supports_long,
            "supports_short": self.supports_short,
            "supports_margin": self.supports_margin,
            "supports_reduce_only": self.supports_reduce_only,
            "has_expiry": self.has_expiry,
            "has_funding": self.has_funding,
            "has_corporate_actions": self.has_corporate_actions,
            "supports_market_order": self.supports_market_order,
            "supports_limit_order": self.supports_limit_order,
            "supports_post_only": self.supports_post_only,
            "trading_session_type": self.trading_session_type.value,
            "settlement_type": self.settlement_type.value,
        }


#: CRYPTO PERPETUAL 的能力（P0001.15 §4 明文给定的当前产品语义）。
CRYPTO_PERPETUAL_CAPABILITIES = InstrumentCapabilities(
    supports_long=True,
    supports_short=True,
    supports_margin=True,
    supports_reduce_only=True,
    has_expiry=False,
    has_funding=True,
    has_corporate_actions=False,
    supports_market_order=True,
    supports_limit_order=True,
    supports_post_only=True,
    trading_session_type=TradingSessionType.CONTINUOUS,
    settlement_type=SettlementType.PERPETUAL_FUNDING,
)


__all__ = [
    "CRYPTO_PERPETUAL_CAPABILITIES", "InstrumentCapabilities", "SettlementType", "TradingSessionType",
]
