"""Risk 领域的契约类型：OrderProposal / RiskSnapshot 输入 / RiskDecision。

本阶段 Risk 只定义**输入契约**与**判定输出**，不实现真实 Order（那是 P0001.6）。
`OrderProposal` 不是 Order：没有 exchange_order_id / status / fills。

判定输出不是裸 bool，而是带 `reason_code` 的 `RiskDecision`，以便 telemetry 统计「到底是谁挡掉了订单」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from market.events.types import Milliseconds
from portfolio.types import LiquidationInfo, Side


class RiskError(Exception):
    """Risk 领域错误基类。"""


class InvalidOrderProposalError(RiskError):
    """OrderProposal 构造失败。"""


class KillSwitchMode(Enum):
    """Kill switch 语义（P0001.6 §16 正式拆分）。"""

    NORMAL = "NORMAL"
    #: 只允许真正降低当前 exposure 的 proposal
    REDUCE_ONLY = "REDUCE_ONLY"
    #: 禁止新 submit（仍允许 cancel 已存在订单）
    HALT_ALL = "HALT_ALL"


class ExposureClass(Enum):
    """订单对暴露的影响方向（§11：提高风险与降低风险不能同等对待）。"""

    INCREASING = "increasing"
    REDUCING = "reducing"
    #: 反向且数量超过当前持仓 → 既有平仓也有开仓，gross 暴露增大
    REVERSING = "reversing"


class RiskReasonCode(Enum):
    """拒绝原因码（第一个命中的检查决定 reason_code）。"""

    # 硬检查（reduce-only 也不能绕过）
    INVALID_ORDER_QUANTITY = "INVALID_ORDER_QUANTITY"
    INVALID_ORDER_PRICE = "INVALID_ORDER_PRICE"
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    KILL_SWITCH = "KILL_SWITCH"
    BOOK_UNHEALTHY = "BOOK_UNHEALTHY"
    MISSING_MARK_PRICE = "MISSING_MARK_PRICE"
    STALE_MARK = "STALE_MARK"
    REDUCE_ONLY_WOULD_INCREASE = "REDUCE_ONLY_WOULD_INCREASE"
    # 仅对增加暴露的订单生效
    POSITION_LIMIT = "POSITION_LIMIT"
    NOTIONAL_LIMIT = "NOTIONAL_LIMIT"
    OPEN_ORDER_EXPOSURE_LIMIT = "OPEN_ORDER_EXPOSURE_LIMIT"
    INSUFFICIENT_AVAILABLE_BALANCE = "INSUFFICIENT_AVAILABLE_BALANCE"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    LEVERAGE_LIMIT = "LEVERAGE_LIMIT"
    LIQUIDATION_DISTANCE = "LIQUIDATION_DISTANCE"
    # 已配置限额但数据缺失 → fail closed
    MISSING_DAILY_PNL = "MISSING_DAILY_PNL"
    MISSING_LIQUIDATION_INFO = "MISSING_LIQUIDATION_INFO"


class RiskDecisionType(Enum):
    ALLOW = "allow"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class OrderProposal:
    """风险输入契约（不是 Order）。

    数量 / 价格只校验类型：非法的数量或价格属于 RiskGate 的硬检查（§11），
    这样 reduce-only 也无法绕过它们。
    """

    symbol: str
    side: Side
    quantity: float
    price: float
    reduce_only: bool = False
    post_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise InvalidOrderProposalError("OrderProposal.symbol must be a non-empty string")
        if not isinstance(self.side, Side):
            raise InvalidOrderProposalError(f"OrderProposal.side must be a Side, got {type(self.side).__name__}")
        for field in ("quantity", "price"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise InvalidOrderProposalError(f"OrderProposal.{field} must be a finite number")
            object.__setattr__(self, field, float(value))

    @property
    def signed_quantity(self) -> float:
        return self.side.sign * self.quantity

    @property
    def notional(self) -> float:
        """按订单自身价格计的名义价值（用于比较限额）。"""
        return self.price * self.quantity

    @property
    def increases_exposure(self) -> bool:
        return self.signed_quantity != 0.0 and not self.reduce_only


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """RiskGate 输出。"""

    decision: RiskDecisionType
    reason_code: RiskReasonCode | None
    details: str
    checks_applied: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision is RiskDecisionType.ALLOW

    @property
    def rejected(self) -> bool:
        return self.decision is RiskDecisionType.REJECT


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    """RiskGate 唯一消费的不可变快照（§8）。

    `None` 表示**未知**（不是 0）：调用方与 gate 必须 fail closed。
    """

    symbol: str
    now_ms: Milliseconds
    balance: float
    equity: float | None
    position_qty: float
    position_notional: float | None
    gross_exposure: float | None
    net_exposure: float | None
    open_order_exposure: float
    available_balance: float
    realized_pnl_today: float | None
    unrealized_pnl: float | None
    drawdown: float | None
    mark_price: float | None
    # 追加字段（供限额判定与审计）
    peak_equity: float | None = None
    drawdown_pct: float | None = None
    mark_age_ms: Milliseconds | None = None
    liquidation: LiquidationInfo | None = None
    trading_fees: float = 0.0
    funding: float = 0.0
    net_realized: float = 0.0


__all__ = [
    "ExposureClass",
    "KillSwitchMode",
    "InvalidOrderProposalError",
    "OrderProposal",
    "RiskDecision",
    "RiskDecisionType",
    "RiskError",
    "RiskReasonCode",
    "RiskSnapshot",
]
