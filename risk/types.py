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


class AvailableBalanceSource(Enum):
    """`RiskSnapshot.available_balance` 的**来源**（P0001.9.4 §3）。

    本地推导只适用于 PAPER / REPLAY；真实下单前的 readiness 必须要求交易所事实。
    """

    #: `accounting.balance - open_order_exposure`（P0001.5 起的既有语义，Paper/Replay 不变）
    LOCAL_DERIVED = "local_derived"
    #: 交易所 `availableBalance`（Binance 权威口径）
    BINANCE_ACCOUNT_SNAPSHOT = "binance_account_snapshot"


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
    #: 已配置 drawdown 限额但历史峰值未知（例如 startup baseline 之后）→ fail closed（P0001.9.3）
    MISSING_DRAWDOWN = "MISSING_DRAWDOWN"
    MISSING_LIQUIDATION_INFO = "MISSING_LIQUIDATION_INFO"
    #: 存在资料不足、无法量化的订单暴露 → 不允许新增暴露（P0001.6.1）
    UNCERTAIN_EXPOSURE_UNKNOWN = "UNCERTAIN_EXPOSURE_UNKNOWN"


class RiskDecisionType(Enum):
    ALLOW = "allow"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class OrderCorrelation:
    """订单的 canonical correlation metadata（P0001.15 §15 / SC-26 + 人类裁决 2）。

    随 `OrderProposal` 进入**提交边界**，与 `client_order_id` 一起进入 canonical order record；
    **不是** runtime 侧临时映射，重启（durable read）后仍应可关联。
    """

    decision_id: str
    instrument_id: str | None = None
    venue_id: str | None = None
    prediction_id: str | None = None
    market_state_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, str) or not self.decision_id:
            raise InvalidOrderProposalError("OrderCorrelation.decision_id must be a non-empty string")
        for name in ("instrument_id", "venue_id", "prediction_id", "market_state_hash"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise InvalidOrderProposalError(f"OrderCorrelation.{name} must be a non-empty string or None")

    def view(self) -> dict[str, object]:
        return {"decision_id": self.decision_id, "instrument_id": self.instrument_id,
                "venue_id": self.venue_id, "prediction_id": self.prediction_id,
                "market_state_hash": self.market_state_hash}


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
    #: canonical correlation metadata（P0001.15 §15）；缺省 None = UNKNOWN（不伪造 id）
    correlation: OrderCorrelation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise InvalidOrderProposalError("OrderProposal.symbol must be a non-empty string")
        if self.correlation is not None and not isinstance(self.correlation, OrderCorrelation):
            raise InvalidOrderProposalError("OrderProposal.correlation must be an OrderCorrelation")
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
class ExchangeAvailableBalance:
    """交易所口径的可用余额事实（**不是**本地估算）。

    携带 `captured_at` 以便计算 `age_ms`：readiness 必须判断新鲜度，而不是只看数值。
    """

    value: float
    captured_at: Milliseconds
    source: AvailableBalanceSource = AvailableBalanceSource.BINANCE_ACCOUNT_SNAPSHOT

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValueError("ExchangeAvailableBalance.value must be a number")
        if not math.isfinite(float(self.value)) or float(self.value) < 0.0:
            raise ValueError("ExchangeAvailableBalance.value must be a non-negative finite number")
        object.__setattr__(self, "value", float(self.value))
        if isinstance(self.captured_at, bool) or not isinstance(self.captured_at, int) or self.captured_at < 0:
            raise ValueError("ExchangeAvailableBalance.captured_at must be a non-negative int epoch-ms")
        if self.source is not AvailableBalanceSource.BINANCE_ACCOUNT_SNAPSHOT:
            raise ValueError(
                "ExchangeAvailableBalance.source must be BINANCE_ACCOUNT_SNAPSHOT "
                "(本地推导不得伪装成交易所事实)"
            )

    def age_ms(self, *, now_ms: Milliseconds) -> int:
        return max(0, now_ms - self.captured_at)


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
    #: `confirmed + uncertain`（P0001.6.1）
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
    #: ACTIVE 订单的未成交名义价值（P0001.6.1）
    confirmed_open_exposure: float = 0.0
    #: LOST 订单的未成交名义价值（P0001.6.1：状态不确定仍占用风险额度）
    uncertain_exposure: float = 0.0
    #: 资料不足、无法量化的订单数量（> 0 时 Gate 拒绝新增暴露）
    unresolved_order_count: int = 0
    #: `available_balance` 的来源（P0001.9.4 §3）；默认保持 P0001.5 的本地推导语义
    available_balance_source: AvailableBalanceSource = AvailableBalanceSource.LOCAL_DERIVED
    #: 交易所事实的采集时刻 / 新鲜度（仅当来源为交易所快照时非 None）
    available_balance_captured_at: Milliseconds | None = None
    available_balance_age_ms: Milliseconds | None = None

    @property
    def has_exchange_available_balance(self) -> bool:
        return self.available_balance_source is AvailableBalanceSource.BINANCE_ACCOUNT_SNAPSHOT


__all__ = [
    "AvailableBalanceSource",
    "ExchangeAvailableBalance",
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
