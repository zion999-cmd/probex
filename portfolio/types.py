"""账户领域的核心类型：Fill / Funding / 强平信息。

`Fill` 是不可变的成交事实；accounting 领域内的 Position / PnL / Balance 全部由它派生
（P0001.5 §1：不允许 Strategy 直接修改 Position）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from market.events.types import Milliseconds, Venue


class Side(Enum):
    """买卖方向。Accounting / Risk 共用同一词表。"""

    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> float:
        return 1.0 if self is Side.BUY else -1.0


class PortfolioError(Exception):
    """账户领域错误基类。"""


class InvalidFillError(PortfolioError):
    """Fill 不满足不变量。"""


class UnsupportedAssetError(PortfolioError):
    """出现非结算资产的费用 —— 本阶段不做多币种换算，fail closed。"""


class InvalidFundingError(PortfolioError):
    """Funding 记录不满足不变量。"""


class InvalidLiquidationInfoError(PortfolioError):
    """强平信息不满足不变量。"""


def _require_positive(value: object, *, field: str, error: type[PortfolioError] = PortfolioError) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise error(f"{field} must be a positive finite number, got {value!r}")
    return number


def _require_non_negative(value: object, *, field: str, error: type[PortfolioError] = PortfolioError) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise error(f"{field} must be a non-negative finite number, got {value!r}")
    return number


def _require_timestamp(value: object, *, field: str, error: type[PortfolioError] = PortfolioError) -> Milliseconds:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error(f"{field} must be an int epoch-millisecond value")
    if value < 0:
        raise error(f"{field} must be >= 0, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class Fill:
    """一笔成交（不可变）。

    `fee` 为正数表示支出；`fee_asset` 必须与账户结算资产一致（不做换算）。
    """

    fill_id: str
    order_id: str
    venue: Venue
    symbol: str
    side: Side
    price: float
    quantity: float
    fee: float
    fee_asset: str
    trade_id: str
    exchange_ts: Milliseconds
    receive_ts: Milliseconds

    def __post_init__(self) -> None:
        for field in ("fill_id", "order_id", "symbol", "trade_id", "fee_asset"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value:
                raise InvalidFillError(f"Fill.{field} must be a non-empty string")
        if not isinstance(self.side, Side):
            raise InvalidFillError(f"Fill.side must be a Side, got {type(self.side).__name__}")
        object.__setattr__(self, "price", _require_positive(self.price, field="Fill.price", error=InvalidFillError))
        object.__setattr__(
            self, "quantity", _require_positive(self.quantity, field="Fill.quantity", error=InvalidFillError)
        )
        object.__setattr__(self, "fee", _require_non_negative(self.fee, field="Fill.fee", error=InvalidFillError))
        object.__setattr__(
            self,
            "exchange_ts",
            _require_timestamp(self.exchange_ts, field="Fill.exchange_ts", error=InvalidFillError),
        )
        object.__setattr__(
            self,
            "receive_ts",
            _require_timestamp(self.receive_ts, field="Fill.receive_ts", error=InvalidFillError),
        )

    @property
    def signed_quantity(self) -> float:
        """BUY 为正、SELL 为负。"""
        return self.side.sign * self.quantity

    @property
    def notional(self) -> float:
        """成交名义价值（不含费用）。"""
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class FundingPayment:
    """一笔资金费（资金费不是 Fill，也不是 Fee，也不是已实现交易盈亏）。"""

    symbol: str
    amount: float
    asset: str
    timestamp: Milliseconds
    rate: float | None = None

    def __post_init__(self) -> None:
        for field in ("symbol", "asset"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value:
                raise InvalidFundingError(f"FundingPayment.{field} must be a non-empty string")
        if isinstance(self.amount, bool) or not isinstance(self.amount, (int, float)):
            raise InvalidFundingError("FundingPayment.amount must be a number")
        amount = float(self.amount)
        if not math.isfinite(amount):
            raise InvalidFundingError(f"FundingPayment.amount must be finite, got {self.amount!r}")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(
            self,
            "timestamp",
            _require_timestamp(self.timestamp, field="FundingPayment.timestamp", error=InvalidFundingError),
        )
        if self.rate is not None:
            if isinstance(self.rate, bool) or not isinstance(self.rate, (int, float)):
                raise InvalidFundingError("FundingPayment.rate must be a number or None")
            rate = float(self.rate)
            if not math.isfinite(rate):
                raise InvalidFundingError(f"FundingPayment.rate must be finite, got {self.rate!r}")
            object.__setattr__(self, "rate", rate)


@dataclass(frozen=True, slots=True)
class LiquidationInfo:
    """强平信息。

    本阶段**不自行推导** Binance 强平价：`liquidation_price` 必须由未来的 account adapter 提供，
    这里只做一致性校验与 `distance_bps` 派生。

    正数 `distance_bps` 表示 mark 到强平价的距离（相对 mark 的万分之一）。
    """

    symbol: str
    liquidation_price: float
    mark_price: float
    distance_bps: float

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise InvalidLiquidationInfoError("LiquidationInfo.symbol must be a non-empty string")
        object.__setattr__(
            self,
            "liquidation_price",
            _require_positive(
                self.liquidation_price,
                field="LiquidationInfo.liquidation_price",
                error=InvalidLiquidationInfoError,
            ),
        )
        object.__setattr__(
            self,
            "mark_price",
            _require_positive(self.mark_price, field="LiquidationInfo.mark_price", error=InvalidLiquidationInfoError),
        )
        distance = self.distance_bps
        if isinstance(distance, bool) or not isinstance(distance, (int, float)):
            raise InvalidLiquidationInfoError("LiquidationInfo.distance_bps must be a number")
        distance = float(distance)
        if not math.isfinite(distance) or distance < 0.0:
            raise InvalidLiquidationInfoError(
                f"LiquidationInfo.distance_bps must be non-negative and finite, got {self.distance_bps!r}"
            )
        expected = abs(self.mark_price - self.liquidation_price) / self.mark_price * 10_000
        if abs(expected - distance) > 1e-6:
            raise InvalidLiquidationInfoError(
                f"LiquidationInfo.distance_bps {distance} is inconsistent with prices "
                f"(expected {expected})"
            )
        object.__setattr__(self, "distance_bps", distance)

    @classmethod
    def from_prices(
        cls, *, symbol: str, liquidation_price: float, mark_price: float
    ) -> LiquidationInfo:
        """由交易所给出的强平价与 mark price 派生 `distance_bps`。"""
        mark = _require_positive(mark_price, field="mark_price", error=InvalidLiquidationInfoError)
        liquidation = _require_positive(
            liquidation_price, field="liquidation_price", error=InvalidLiquidationInfoError
        )
        distance = abs(mark - liquidation) / mark * 10_000
        return cls(
            symbol=symbol,
            liquidation_price=liquidation,
            mark_price=mark,
            distance_bps=distance,
        )

    @property
    def is_below_mark(self) -> bool:
        """强平价是否位于 mark 下方（long 仓位通常是这种情况）。"""
        return self.liquidation_price < self.mark_price


__all__ = [
    "Fill",
    "FundingPayment",
    "InvalidFillError",
    "InvalidFundingError",
    "InvalidLiquidationInfoError",
    "LiquidationInfo",
    "PortfolioError",
    "Side",
    "UnsupportedAssetError",
]
