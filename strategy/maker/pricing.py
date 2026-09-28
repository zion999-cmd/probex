"""QuotePrice：只允许「挂盘口或向后退」的被动挂价（P0001.7 §0.2）。

硬不变量：

- 买价 ≤ best_bid、卖价 ≥ best_ask（永不穿价、永不做 taker）；
- 价格对齐 `tick_size` 网格（买向下取整、卖向上取整）；
- 新增暴露的报价必须满足 cost floor `minimum_edge_bps`（相对 mid 的方向性优势）；
- reduce-only 报价豁免 cost floor（它只降低暴露），但仍受穿价不变量约束。

后退 tick 的来源（每个原因贡献 1 tick，总计受 `max_back_ticks` 限制）：
adverse selection ≥ retreat 阈值、失衡不利、microprice skew 不利、预测不利方向概率 ≥ 阈值、
以及库存偏置带来的额外后退。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from market.state.types import MarketState
from portfolio.types import Side
from prediction.types import Prediction
from strategy.maker.types import MakerPolicyConfig, QuoteTrigger

#: 浮点比较容差（tick 对齐 / 边际门槛都在同一个量级上比较）。
_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class PricePlan:
    """单侧挂价计划。`permitted=False` 时 `price` 为 None。"""

    side: Side
    reference_price: float | None
    price: float | None
    retreat_ticks: int
    edge_bps: float | None
    permitted: bool
    trigger: QuoteTrigger | None
    reasons: tuple[str, ...]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _is_finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0.0


def _edge_bps(*, side: Side, price: float, mid: float) -> float:
    """相对 mid 的方向性优势（bps）：买价低于 mid / 卖价高于 mid 为正。"""
    return side.sign * (mid - price) / mid * 10_000.0


def _round_to_tick(*, side: Side, price: float, tick_size: float) -> float:
    """买向下取整、卖向上取整：只可能更加被动，不可能穿价。"""
    units = price / tick_size
    adjusted = math.floor(units + _EPSILON) if side is Side.BUY else math.ceil(units - _EPSILON)
    return adjusted * tick_size


def _retreat_reasons(
    *,
    side: Side,
    state: MarketState,
    prediction: Prediction | None,
    config: MakerPolicyConfig,
    extra_retreat_ticks: int,
) -> tuple[tuple[str, ...], int]:
    """收集后退原因；每个原因贡献 1 tick。缺失 feature 不产生 tick（可用性由质量门负责）。"""
    reasons: list[str] = []
    ticks = max(0, extra_retreat_ticks)
    if extra_retreat_ticks > 0:
        reasons.append(f"inventory_retreat_ticks={extra_retreat_ticks}")

    imbalance = state.depth.depth_imbalance_5
    if imbalance is not None and side.sign * imbalance <= -config.imbalance_retreat:
        ticks += 1
        reasons.append(f"depth_imbalance_5={imbalance:.6f} adverse")

    microprice = state.price.microprice
    mid = state.price.mid
    if microprice is not None and mid is not None and mid > 0.0:
        skew_bps = (microprice - mid) / mid * 10_000.0
        if side.sign * skew_bps <= -config.microprice_skew_retreat_bps:
            ticks += 1
            reasons.append(f"microprice_skew_bps={skew_bps:.6f} adverse")

    if prediction is not None:
        adverse = prediction.buy_adverse_selection if side is Side.BUY else prediction.sell_adverse_selection
        if adverse >= config.adverse_selection_retreat:
            ticks += 1
            reasons.append(f"adverse_selection={adverse:.6f}")
        if _predicted_move_against(prediction, side, config.prediction_horizon_ms) >= config.predicted_move_retreat:
            ticks += 1
            reasons.append("predicted_move_against")

    return tuple(reasons), min(ticks, config.max_back_ticks)


def _predicted_move_against(prediction: Prediction, side: Side, horizon_ms: int) -> float:
    """指定 horizon 上「不利于该侧」的累计概率；缺 horizon 时视为不利（fail closed）。"""
    distribution = prediction.distribution(horizon_ms)
    if distribution is None:
        return 1.0
    if side is Side.BUY:
        return distribution.down + distribution.strong_down
    return distribution.up + distribution.strong_up


def _required_retreat_ticks(
    *,
    side: Side,
    reference: float,
    mid: float,
    ticks: int,
    config: MakerPolicyConfig,
    reduce_only: bool,
) -> int:
    """把 cost floor 换成最少后退 tick 数（解析解，不做迭代）。"""
    if reduce_only or config.minimum_edge_bps <= 0.0:
        return ticks
    required_distance = mid * config.minimum_edge_bps / 10_000.0
    target = mid - side.sign * required_distance
    needed = math.ceil(((reference - target) * side.sign) / config.tick_size - _EPSILON)
    return min(max(ticks, max(0, needed)), config.max_back_ticks)


def _blocked(
    *,
    side: Side,
    reference_price: float | None,
    trigger: QuoteTrigger,
    reasons: tuple[str, ...],
    retreat_ticks: int = 0,
    edge_bps: float | None = None,
) -> PricePlan:
    return PricePlan(
        side=side,
        reference_price=reference_price,
        price=None,
        retreat_ticks=retreat_ticks,
        edge_bps=edge_bps,
        permitted=False,
        trigger=trigger,
        reasons=reasons,
    )


def plan_quote_price(
    *,
    side: Side,
    state: MarketState,
    prediction: Prediction | None,
    config: MakerPolicyConfig,
    reduce_only: bool,
    extra_retreat_ticks: int = 0,
) -> PricePlan:
    """计算单侧被动挂价。`prediction=None` 只能配合 `reduce_only=True`（新增暴露由 policy 层挡住）。"""
    reference = state.price.best_bid if side is Side.BUY else state.price.best_ask
    mid = state.price.mid
    if not _is_finite_positive(reference) or not _is_finite_positive(mid):
        return _blocked(
            side=side,
            reference_price=reference,
            trigger=QuoteTrigger.MARKET_DATA_UNAVAILABLE,
            reasons=("best_bid/best_ask/mid unavailable",),
        )
    assert reference is not None and mid is not None  # narrowed by _is_finite_positive

    if prediction is not None:
        adverse = prediction.buy_adverse_selection if side is Side.BUY else prediction.sell_adverse_selection
        if adverse >= config.adverse_selection_block:
            return _blocked(
                side=side,
                reference_price=reference,
                trigger=QuoteTrigger.ADVERSE_SELECTION,
                reasons=(f"adverse_selection={adverse:.6f} >= block {config.adverse_selection_block:.6f}",),
            )

    reasons, ticks = _retreat_reasons(
        side=side,
        state=state,
        prediction=prediction,
        config=config,
        extra_retreat_ticks=extra_retreat_ticks,
    )
    ticks = _required_retreat_ticks(
        side=side, reference=reference, mid=mid, ticks=ticks, config=config, reduce_only=reduce_only
    )
    return _finalize_price(
        side=side,
        reference=reference,
        mid=mid,
        ticks=ticks,
        reasons=reasons,
        config=config,
        reduce_only=reduce_only,
    )


def _finalize_price(
    *,
    side: Side,
    reference: float,
    mid: float,
    ticks: int,
    reasons: tuple[str, ...],
    config: MakerPolicyConfig,
    reduce_only: bool,
) -> PricePlan:
    """按 tick 网格取被动价，并判定 cost floor。"""
    price = _round_to_tick(side=side, price=reference - side.sign * ticks * config.tick_size, tick_size=config.tick_size)

    if price <= 0.0:
        return _blocked(
            side=side,
            reference_price=reference,
            trigger=QuoteTrigger.SIDE_NOT_PERMITTED,
            reasons=reasons + ("price after retreat would be non-positive",),
            retreat_ticks=ticks,
        )

    edge_bps = _edge_bps(side=side, price=price, mid=mid)
    if not reduce_only and edge_bps + _EPSILON < config.minimum_edge_bps:
        return _blocked(
            side=side,
            reference_price=reference,
            trigger=QuoteTrigger.COST_FLOOR_UNMET,
            reasons=reasons + (f"edge_bps={edge_bps:.6f} < minimum_edge_bps={config.minimum_edge_bps:.6f}",),
            retreat_ticks=ticks,
            edge_bps=edge_bps,
        )
    return PricePlan(
        side=side,
        reference_price=reference,
        price=price,
        retreat_ticks=ticks,
        edge_bps=edge_bps,
        permitted=True,
        trigger=None,
        reasons=reasons,
    )


__all__ = ["PricePlan", "plan_quote_price"]
