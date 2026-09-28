"""QuoteSize：把基础下单量按有界乘数缩放（P0001.7 §0.2 / §0.4）。

```text
quantity = base_size × inventory_factor × confidence_factor × risk_budget_factor
```

- 所有 factor 都有上下界：confidence 再高也不会无限放大；
- **fill probability 不进入数量公式**（SC-5：高 fill probability 可能正是 adverse selection 高），
  Prediction 对数量的影响只通过有界的 `confidence_factor`（`derived_confidence`）表达；
- 新增暴露必须有已知且未耗尽的 `remaining_risk_budget`（未知 ≠ 0 → fail closed）；
- reduce-only 数量被 `|position_qty − target_position|` 截断，保证不会变成增加暴露。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from portfolio.types import Side
from strategy.maker.types import MakerPolicyConfig, QuoteTrigger

_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class SizePlan:
    """单侧挂量计划。`permitted=False` 时 `quantity` 为 None。"""

    side: Side
    quantity: float | None
    confidence_factor: float
    inventory_factor: float
    risk_budget_factor: float
    permitted: bool
    trigger: QuoteTrigger | None
    reasons: tuple[str, ...]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _floor_to_step(quantity: float, step: float) -> float:
    units = math.floor(quantity / step + _EPSILON)
    return max(0.0, units * step)


def confidence_factor(*, derived_confidence: float, config: MakerPolicyConfig) -> float:
    """把 `derived_confidence` 线性映射到 `[confidence_factor_min, 1]`（有界）。"""
    span = config.confidence_ref_high - config.confidence_ref_low
    normalized = _clamp((float(derived_confidence) - config.confidence_ref_low) / span, 0.0, 1.0)
    return config.confidence_factor_min + (1.0 - config.confidence_factor_min) * normalized


def _resolve_budget_factor(
    *,
    reduce_only: bool,
    remaining_risk_budget: float | None,
    intended_notional: float,
    config: MakerPolicyConfig,
) -> tuple[float | None, QuoteTrigger | None, str]:
    """返回 (factor, 阻止原因, 详情)；`reduce_only` 不需要预算。"""
    if reduce_only:
        return 1.0, None, ""
    if remaining_risk_budget is None:
        return None, QuoteTrigger.RISK_BUDGET_UNKNOWN, "remaining_risk_budget is unknown; new exposure is not allowed (fail closed)"
    if remaining_risk_budget <= 0.0:
        return (
            None,
            QuoteTrigger.RISK_BUDGET_EXHAUSTED,
            f"remaining_risk_budget={remaining_risk_budget:.10g} leaves no room for new exposure",
        )
    return _clamp(remaining_risk_budget / intended_notional, config.risk_factor_min, 1.0), None, ""


def plan_quote_size(
    *,
    side: Side,
    price: float,
    inventory_factor: float,
    derived_confidence: float,
    remaining_risk_budget: float | None,
    reduce_only: bool,
    position_qty: float,
    config: MakerPolicyConfig,
) -> SizePlan:
    """计算单侧下单量（纯函数，确定性）。"""
    confidence = confidence_factor(derived_confidence=derived_confidence, config=config)
    # 库存乘数也在此处 clamp：即使调用方传入越界值，也不可能放大到无界
    inventory = _clamp(float(inventory_factor), config.size_factor_min, config.size_factor_max)
    intended_notional = config.base_size * inventory * confidence * price

    budget_factor, blocked_trigger, blocked_detail = _resolve_budget_factor(
        reduce_only=reduce_only,
        remaining_risk_budget=remaining_risk_budget,
        intended_notional=intended_notional,
        config=config,
    )
    if blocked_trigger is not None or budget_factor is None:
        return _blocked(side, confidence, inventory, 0.0, blocked_trigger or QuoteTrigger.SIDE_NOT_PERMITTED, blocked_detail)

    raw = config.base_size * inventory * confidence * budget_factor
    capped = _cap_to_position(raw, reduce_only=reduce_only, position_qty=position_qty, config=config)
    quantity = _floor_to_step(capped, config.quantity_step)
    if quantity < config.min_quote_size:
        return _blocked(
            side,
            confidence,
            inventory,
            budget_factor,
            QuoteTrigger.SIZE_BELOW_MINIMUM,
            f"quantity {quantity:.10g} is below min_quote_size {config.min_quote_size:.10g}",
        )
    return _permitted(side, quantity, confidence, inventory, budget_factor, config)


def _permitted(
    side: Side,
    quantity: float,
    confidence: float,
    inventory: float,
    budget_factor: float,
    config: MakerPolicyConfig,
) -> SizePlan:
    return SizePlan(
        side=side,
        quantity=quantity,
        confidence_factor=confidence,
        inventory_factor=inventory,
        risk_budget_factor=budget_factor,
        permitted=True,
        trigger=None,
        reasons=(
            f"base_size={config.base_size:.10g}",
            f"confidence_factor={confidence:.6f}",
            f"inventory_factor={inventory:.6f}",
            f"risk_budget_factor={budget_factor:.6f}",
        ),
    )


def _cap_to_position(
    raw: float, *, reduce_only: bool, position_qty: float, config: MakerPolicyConfig
) -> float:
    """reduce-only 数量不得超过「把持仓推到 target」所需的数量。"""
    if not reduce_only:
        return raw
    return min(raw, abs(float(position_qty) - config.target_position))


def _blocked(
    side: Side,
    confidence: float,
    inventory_factor: float,
    budget_factor: float,
    trigger: QuoteTrigger,
    detail: str,
) -> SizePlan:
    return SizePlan(
        side=side,
        quantity=None,
        confidence_factor=confidence,
        inventory_factor=inventory_factor,
        risk_budget_factor=budget_factor,
        permitted=False,
        trigger=trigger,
        reasons=(detail,),
    )


__all__ = ["SizePlan", "confidence_factor", "plan_quote_size"]
