"""Maker 策略层的契约类型（P0001.7）。

本模块只描述**策略决策**，不描述订单生命周期：

- `MakerPolicyConfig`：全部阈值 / 尺寸 / 周期由调用方注入，**无默认值**（§12）。
- `QuoteDecision`：某一侧在本轮决策中的动作（PLACE / KEEP / CANCEL / REPLACE / NONE）。
- `MakerDecision`：双边决策 + 期望报价姿态（both / bid only / ask only / none）。

策略层输出永远不包含交易所交互；真正的撤单与重挂由 P0001.6 的 `OrderManager` 执行。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from market.events.types import Milliseconds
from portfolio.types import Side
from risk.types import OrderProposal


class QuoteAction(Enum):
    """单侧动作。

    `PLACE` / `NONE` 是提案要求的 KEEP / CANCEL / REPLACE 之外的必要补充（§0.8）：
    否则无法表达「该侧还没有挂单」与「该侧不应有挂单」。
    """

    #: 该侧当前无挂单，需要新增。
    PLACE = "place"
    #: 现有挂单保持不动。
    KEEP = "keep"
    #: 撤掉现有挂单，不重挂。
    CANCEL = "cancel"
    #: 撤掉现有挂单，待其确认终态后由 OrderManager 重挂（cancel-before-replace）。
    REPLACE = "replace"
    #: 该侧不应有挂单，且当前也没有。
    NONE = "none"


class QuoteMode(Enum):
    """期望报价姿态。"""

    BOTH = "both"
    BID_ONLY = "bid_only"
    ASK_ONLY = "ask_only"
    NONE = "none"


class QuoteTrigger(Enum):
    """本轮动作的主导原因（telemetry / 审计用）。"""

    INITIAL_QUOTE = "initial_quote"
    WITHIN_TOLERANCE = "within_tolerance"
    PREDICTION_STALE = "prediction_stale"
    MARKET_UNHEALTHY = "market_unhealthy"
    MARKET_DATA_UNAVAILABLE = "market_data_unavailable"
    KILL_SWITCH = "kill_switch"
    UNKNOWN_EXPOSURE = "unknown_exposure"
    PRICE_MOVED = "price_moved"
    QUOTE_AGE_EXCEEDED = "quote_age_exceeded"
    SIZE_DRIFT = "size_drift"
    SIDE_NOT_PERMITTED = "side_not_permitted"
    UNKNOWN_ORDER_STATE = "unknown_order_state"
    CANCEL_IN_FLIGHT = "cancel_in_flight"
    RISK_BUDGET_UNKNOWN = "risk_budget_unknown"
    RISK_BUDGET_EXHAUSTED = "risk_budget_exhausted"
    COST_FLOOR_UNMET = "cost_floor_unmet"
    ADVERSE_SELECTION = "adverse_selection"
    SIZE_BELOW_MINIMUM = "size_below_minimum"
    #: 全局门禁止新增暴露，但该侧现有挂单是 reduce-only → 保留（降风险是安全的）。
    KEEP_REDUCE_ONLY = "keep_reduce_only"


def _require_positive(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{field} must be a positive finite number, got {value!r}")
    return number


def _require_non_negative(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{field} must be a non-negative finite number, got {value!r}")
    return number


def _require_fraction(value: object, *, field: str) -> float:
    number = _require_non_negative(value, field=field)
    if number > 1.0:
        raise ValueError(f"{field} must be in [0, 1], got {number}")
    return number


def _require_int(value: object, *, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an int >= {minimum}, got {value!r}")
    return value


def _require_finite(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number, got {value!r}")
    return number


@dataclass(frozen=True, slots=True)
class MakerPolicyConfig:
    """Maker 策略的全部注入参数（无默认值）。"""

    # 交易所网格与实际下单量
    tick_size: float
    quantity_step: float
    min_quote_size: float
    base_size: float
    # 挂价
    max_back_ticks: int
    minimum_edge_bps: float
    # prediction 门与后退阈值
    prediction_horizon_ms: Milliseconds
    adverse_selection_retreat: float
    adverse_selection_block: float
    imbalance_retreat: float
    microprice_skew_retreat_bps: float
    predicted_move_retreat: float
    # 库存控制
    target_position: float
    inventory_scale: float
    inventory_size_strength: float
    inventory_retreat_ticks_max: int
    # 乘数上下界
    size_factor_min: float
    size_factor_max: float
    confidence_ref_low: float
    confidence_ref_high: float
    confidence_factor_min: float
    risk_factor_min: float
    # 生命周期
    price_move_ticks_replace: int
    max_quote_age_ms: Milliseconds
    size_drift_tolerance: float

    def __post_init__(self) -> None:
        _normalize_grid(self)
        _normalize_price_params(self)
        _normalize_prediction_thresholds(self)
        _normalize_inventory_params(self)
        _normalize_factor_bounds(self)
        _normalize_lifecycle_params(self)


def _normalize_grid(config: MakerPolicyConfig) -> None:
    for field in ("tick_size", "quantity_step", "min_quote_size", "base_size"):
        object.__setattr__(
            config, field, _require_positive(getattr(config, field), field=f"MakerPolicyConfig.{field}")
        )


def _normalize_price_params(config: MakerPolicyConfig) -> None:
    object.__setattr__(
        config,
        "max_back_ticks",
        _require_int(config.max_back_ticks, field="MakerPolicyConfig.max_back_ticks", minimum=0),
    )
    object.__setattr__(
        config,
        "minimum_edge_bps",
        _require_non_negative(config.minimum_edge_bps, field="MakerPolicyConfig.minimum_edge_bps"),
    )
    object.__setattr__(
        config,
        "prediction_horizon_ms",
        _require_int(config.prediction_horizon_ms, field="MakerPolicyConfig.prediction_horizon_ms", minimum=1),
    )


def _normalize_prediction_thresholds(config: MakerPolicyConfig) -> None:
    block = _require_fraction(config.adverse_selection_block, field="MakerPolicyConfig.adverse_selection_block")
    retreat = _require_fraction(
        config.adverse_selection_retreat, field="MakerPolicyConfig.adverse_selection_retreat"
    )
    if retreat > block:
        raise ValueError(
            "MakerPolicyConfig.adverse_selection_retreat must be <= adverse_selection_block, "
            f"got {retreat} > {block}"
        )
    object.__setattr__(config, "adverse_selection_block", block)
    object.__setattr__(config, "adverse_selection_retreat", retreat)
    object.__setattr__(
        config,
        "imbalance_retreat",
        _require_fraction(config.imbalance_retreat, field="MakerPolicyConfig.imbalance_retreat"),
    )
    object.__setattr__(
        config,
        "microprice_skew_retreat_bps",
        _require_non_negative(
            config.microprice_skew_retreat_bps, field="MakerPolicyConfig.microprice_skew_retreat_bps"
        ),
    )
    object.__setattr__(
        config,
        "predicted_move_retreat",
        _require_fraction(config.predicted_move_retreat, field="MakerPolicyConfig.predicted_move_retreat"),
    )


def _normalize_inventory_params(config: MakerPolicyConfig) -> None:
    object.__setattr__(
        config, "target_position", _require_finite(config.target_position, field="MakerPolicyConfig.target_position")
    )
    object.__setattr__(
        config, "inventory_scale", _require_positive(config.inventory_scale, field="MakerPolicyConfig.inventory_scale")
    )
    object.__setattr__(
        config,
        "inventory_size_strength",
        _require_fraction(config.inventory_size_strength, field="MakerPolicyConfig.inventory_size_strength"),
    )
    object.__setattr__(
        config,
        "inventory_retreat_ticks_max",
        _require_int(
            config.inventory_retreat_ticks_max, field="MakerPolicyConfig.inventory_retreat_ticks_max", minimum=0
        ),
    )


def _normalize_factor_bounds(config: MakerPolicyConfig) -> None:
    size_min = _require_positive(config.size_factor_min, field="MakerPolicyConfig.size_factor_min")
    size_max = _require_positive(config.size_factor_max, field="MakerPolicyConfig.size_factor_max")
    if size_min > size_max:
        raise ValueError(
            f"MakerPolicyConfig.size_factor_min must be <= size_factor_max, got {size_min} > {size_max}"
        )
    object.__setattr__(config, "size_factor_min", size_min)
    object.__setattr__(config, "size_factor_max", size_max)
    ref_low = _require_fraction(config.confidence_ref_low, field="MakerPolicyConfig.confidence_ref_low")
    ref_high = _require_fraction(config.confidence_ref_high, field="MakerPolicyConfig.confidence_ref_high")
    if ref_low >= ref_high:
        raise ValueError(
            f"MakerPolicyConfig.confidence_ref_low must be < confidence_ref_high, got {ref_low} >= {ref_high}"
        )
    object.__setattr__(config, "confidence_ref_low", ref_low)
    object.__setattr__(config, "confidence_ref_high", ref_high)
    confidence_min = _require_positive(config.confidence_factor_min, field="MakerPolicyConfig.confidence_factor_min")
    if confidence_min > 1.0:
        raise ValueError(f"MakerPolicyConfig.confidence_factor_min must be <= 1, got {confidence_min}")
    object.__setattr__(config, "confidence_factor_min", confidence_min)
    risk_min = _require_positive(config.risk_factor_min, field="MakerPolicyConfig.risk_factor_min")
    if risk_min > 1.0:
        raise ValueError(f"MakerPolicyConfig.risk_factor_min must be <= 1, got {risk_min}")
    object.__setattr__(config, "risk_factor_min", risk_min)


def _normalize_lifecycle_params(config: MakerPolicyConfig) -> None:
    object.__setattr__(
        config,
        "price_move_ticks_replace",
        _require_int(config.price_move_ticks_replace, field="MakerPolicyConfig.price_move_ticks_replace", minimum=0),
    )
    object.__setattr__(
        config,
        "max_quote_age_ms",
        _require_int(config.max_quote_age_ms, field="MakerPolicyConfig.max_quote_age_ms", minimum=1),
    )
    object.__setattr__(
        config,
        "size_drift_tolerance",
        _require_non_negative(config.size_drift_tolerance, field="MakerPolicyConfig.size_drift_tolerance"),
    )


@dataclass(frozen=True, slots=True)
class QuoteDecision:
    """某一侧的动作。"""

    side: Side
    action: QuoteAction
    trigger: QuoteTrigger
    client_order_id: str | None = None
    proposal: OrderProposal | None = None
    price: float | None = None
    quantity: float | None = None
    reduce_only: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            raise ValueError(f"QuoteDecision.side must be a Side, got {type(self.side).__name__}")
        if not isinstance(self.action, QuoteAction):
            raise ValueError(f"QuoteDecision.action must be a QuoteAction, got {type(self.action).__name__}")
        if not isinstance(self.trigger, QuoteTrigger):
            raise ValueError(f"QuoteDecision.trigger must be a QuoteTrigger, got {type(self.trigger).__name__}")
        if self.proposal is not None:
            if not self.proposal.post_only:
                raise ValueError("QuoteDecision.proposal must be post_only")
            if self.proposal.side is not self.side:
                raise ValueError("QuoteDecision.proposal.side must match QuoteDecision.side")
            if self.proposal.reduce_only is not self.reduce_only:
                raise ValueError("QuoteDecision.proposal.reduce_only must match QuoteDecision.reduce_only")
        if self.action is QuoteAction.NONE:
            if self.client_order_id is not None or self.proposal is not None:
                raise ValueError("QuoteAction.NONE must not carry an order or a proposal")
        elif self.action is QuoteAction.PLACE:
            if self.client_order_id is not None or self.proposal is None:
                raise ValueError("QuoteAction.PLACE requires a proposal and no existing order")
        elif self.action in (QuoteAction.KEEP, QuoteAction.CANCEL, QuoteAction.REPLACE):
            if not self.client_order_id:
                raise ValueError(f"QuoteAction.{self.action.name} requires the existing client_order_id")
            if (self.proposal is not None) is not (self.action is QuoteAction.REPLACE):
                raise ValueError(f"QuoteAction.{self.action.name} proposal presence is invalid")

    @property
    def requires_cancel(self) -> bool:
        return self.action in (QuoteAction.CANCEL, QuoteAction.REPLACE)

    @property
    def places_order(self) -> bool:
        return self.action in (QuoteAction.PLACE, QuoteAction.REPLACE)


@dataclass(frozen=True, slots=True)
class MakerDecision:
    """一轮 Maker 决策（双边）。"""

    symbol: str
    at_ms: Milliseconds
    bid: QuoteDecision
    ask: QuoteDecision
    bid_desired: bool
    ask_desired: bool
    blocked_by: QuoteTrigger | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("MakerDecision.symbol must be a non-empty string")
        if self.bid.side is not Side.BUY:
            raise ValueError("MakerDecision.bid must be the BUY side decision")
        if self.ask.side is not Side.SELL:
            raise ValueError("MakerDecision.ask must be the SELL side decision")

    @property
    def mode(self) -> QuoteMode:
        if self.bid_desired and self.ask_desired:
            return QuoteMode.BOTH
        if self.bid_desired:
            return QuoteMode.BID_ONLY
        if self.ask_desired:
            return QuoteMode.ASK_ONLY
        return QuoteMode.NONE

    def for_side(self, side: Side) -> QuoteDecision:
        return self.bid if side is Side.BUY else self.ask

    @property
    def sides(self) -> tuple[QuoteDecision, QuoteDecision]:
        return (self.bid, self.ask)

    def quotes(self) -> tuple[OrderProposal, ...]:
        """本轮应当提交的报价（PLACE / REPLACE）。REPLACE 的旧单撤单必须先确认。"""
        return tuple(decision.proposal for decision in self.sides if decision.proposal is not None)

    def cancels(self) -> tuple[str, ...]:
        """本轮应当请求撤单的订单（含 REPLACE 的旧单）。"""
        return tuple(
            decision.client_order_id
            for decision in self.sides
            if decision.requires_cancel and decision.client_order_id is not None
        )


__all__ = [
    "MakerDecision",
    "MakerPolicyConfig",
    "QuoteAction",
    "QuoteDecision",
    "QuoteMode",
    "QuoteTrigger",
]
