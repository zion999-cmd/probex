"""QuoteLifecycle：KEEP / CANCEL / REPLACE / PLACE / NONE（P0001.7 §0.5）。

本模块只做「与现有挂单对比」的决策，不修改任何订单状态：

- `REPLACE` 只表示「撤掉旧单，待其确认终态后重挂」；真正的 cancel-before-replace
  由 P0001.6 的 `OrderManager.replace()` 执行（SC-10）；
- `LOST`（状态未知）既不重复下单、也不假设可撤 → KEEP；
- `PENDING_CANCEL`（撤单已在途中）→ KEEP，避免重复撤单。

触发优先级固定，首个命中者决定动作，因此同一输入永远得到同一输出（SC-12）。
"""

from __future__ import annotations

from dataclasses import dataclass

from execution.types import Order, OrderStatus
from market.events.types import Milliseconds
from portfolio.types import Side
from risk.types import OrderProposal
from strategy.maker.types import MakerPolicyConfig, QuoteAction, QuoteDecision, QuoteTrigger


@dataclass(frozen=True, slots=True)
class SidePlan:
    """策略层对某一侧的**期望**（尚未与现有挂单对比）。"""

    side: Side
    desired_price: float | None
    desired_quantity: float | None
    reduce_only: bool
    forbidden: QuoteTrigger | None
    forbidden_detail: str
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.desired_price is None) is not (self.desired_quantity is None):
            raise ValueError("SidePlan.desired_price and desired_quantity must be set together")

    @property
    def desired(self) -> bool:
        return self.desired_price is not None and self.desired_quantity is not None


def _price_moved(*, existing: Order, desired_price: float, config: MakerPolicyConfig) -> bool:
    moved_ticks = abs(existing.price - desired_price) / config.tick_size
    return moved_ticks + 1e-9 >= config.price_move_ticks_replace


def _age_exceeded(*, existing: Order, now_ms: Milliseconds, config: MakerPolicyConfig) -> bool:
    return max(0, now_ms - existing.created_at) > config.max_quote_age_ms


def _size_drifted(*, existing: Order, desired_quantity: float, desired_reduce_only: bool, config: MakerPolicyConfig) -> bool:
    if existing.reduce_only is not desired_reduce_only:
        return True
    remaining = existing.remaining_quantity
    drift = abs(remaining - desired_quantity)
    return drift > config.size_drift_tolerance * desired_quantity + 1e-9


def plan_side_action(
    *,
    plan: SidePlan,
    existing: Order | None,
    symbol: str,
    config: MakerPolicyConfig,
    now_ms: Milliseconds,
    gate_trigger: QuoteTrigger | None = None,
    cancel_increasing: bool = False,
    cancel_all: bool = False,
) -> QuoteDecision:
    """把「该侧期望」与「该侧现有挂单」合成为一个确定性动作。

    `cancel_increasing`：全局门禁止新增暴露时，撤掉**非 reduce-only** 的现有报价；
    `cancel_all`：操作级熔断（HALT_ALL），撤掉该侧全部报价。
    """
    preemptive = _preemptive_action(
        plan=plan,
        existing=existing,
        symbol=symbol,
        gate_trigger=gate_trigger,
        cancel_increasing=cancel_increasing,
        cancel_all=cancel_all,
    )
    if preemptive is not None:
        return preemptive

    assert existing is not None  # _preemptive_action 覆盖了 existing 为 None / 终态的情况
    assert plan.desired_price is not None and plan.desired_quantity is not None  # plan.desired
    if _price_moved(existing=existing, desired_price=plan.desired_price, config=config):
        return _replace(plan, existing, symbol=symbol, trigger=QuoteTrigger.PRICE_MOVED)
    if _age_exceeded(existing=existing, now_ms=now_ms, config=config):
        return _replace(plan, existing, symbol=symbol, trigger=QuoteTrigger.QUOTE_AGE_EXCEEDED)
    if _size_drifted(
        existing=existing,
        desired_quantity=plan.desired_quantity,
        desired_reduce_only=plan.reduce_only,
        config=config,
    ):
        return _replace(plan, existing, symbol=symbol, trigger=QuoteTrigger.SIZE_DRIFT)
    return _keep(
        plan.side,
        existing,
        trigger=QuoteTrigger.WITHIN_TOLERANCE,
        detail="existing quote is still within price/size/age tolerance",
    )


def _preemptive_action(
    *,
    plan: SidePlan,
    existing: Order | None,
    symbol: str,
    gate_trigger: QuoteTrigger | None,
    cancel_increasing: bool,
    cancel_all: bool,
) -> QuoteDecision | None:
    """先于「期望 vs 现有」比较的判定；返回 None 表示需要继续比较。"""
    if existing is None or existing.is_terminal:
        return _initial(plan, symbol=symbol)
    in_flight = _in_flight_hold(plan.side, existing)
    if in_flight is not None:
        return in_flight
    if cancel_all:
        return _cancel(
            plan,
            existing,
            gate_trigger or QuoteTrigger.KILL_SWITCH,
            plan.forbidden_detail or "all quotes are canceled under HALT_ALL",
        )
    if cancel_increasing and not existing.reduce_only:
        return _cancel(
            plan,
            existing,
            gate_trigger or QuoteTrigger.SIDE_NOT_PERMITTED,
            plan.forbidden_detail or "new exposure is not allowed; cancel the increasing quote",
        )
    if plan.forbidden is not None:
        if existing.reduce_only:
            return _keep(
                plan.side,
                existing,
                trigger=QuoteTrigger.KEEP_REDUCE_ONLY,
                detail=f"new exposure forbidden ({plan.forbidden.value}): keep the risk-reducing quote",
            )
        return _cancel(plan, existing, plan.forbidden, plan.forbidden_detail)
    if not plan.desired:
        return _cancel(plan, existing, QuoteTrigger.SIDE_NOT_PERMITTED, "no quote is desired on this side")
    return None


def _in_flight_hold(side: Side, existing: Order) -> QuoteDecision | None:
    """`LOST`（状态未知）与 `PENDING_CANCEL`（撤单在途）都不允许再次下单/撤单。"""
    if existing.status is OrderStatus.LOST:
        return _keep(
            side,
            existing,
            trigger=QuoteTrigger.UNKNOWN_ORDER_STATE,
            detail="order state is unknown (LOST); do not place another order and do not assume it can be canceled",
        )
    if existing.status is OrderStatus.PENDING_CANCEL:
        return _keep(
            side,
            existing,
            trigger=QuoteTrigger.CANCEL_IN_FLIGHT,
            detail="a cancel request is already in flight; wait for confirmation",
        )
    return None


def _keep(side: Side, existing: Order, *, trigger: QuoteTrigger, detail: str) -> QuoteDecision:
    return QuoteDecision(
        side=side,
        action=QuoteAction.KEEP,
        trigger=trigger,
        client_order_id=existing.client_order_id,
        price=existing.price,
        quantity=existing.remaining_quantity,
        reduce_only=existing.reduce_only,
        detail=detail,
    )


def _proposal(plan: SidePlan, symbol: str) -> OrderProposal:
    assert plan.desired_price is not None and plan.desired_quantity is not None  # plan.desired
    return OrderProposal(
        symbol=symbol,
        side=plan.side,
        quantity=plan.desired_quantity,
        price=plan.desired_price,
        reduce_only=plan.reduce_only,
        post_only=True,
    )


def _initial(plan: SidePlan, *, symbol: str) -> QuoteDecision:
    if plan.desired:
        return QuoteDecision(
            side=plan.side,
            action=QuoteAction.PLACE,
            trigger=QuoteTrigger.INITIAL_QUOTE,
            proposal=_proposal(plan, symbol),
            price=plan.desired_price,
            quantity=plan.desired_quantity,
            reduce_only=plan.reduce_only,
            detail="no resting order on this side; place a passive quote",
        )
    return QuoteDecision(
        side=plan.side,
        action=QuoteAction.NONE,
        trigger=plan.forbidden or QuoteTrigger.SIDE_NOT_PERMITTED,
        detail=plan.forbidden_detail or "no quote is desired on this side",
    )


def _cancel(plan: SidePlan, existing: Order, trigger: QuoteTrigger, detail: str) -> QuoteDecision:
    return QuoteDecision(
        side=plan.side,
        action=QuoteAction.CANCEL,
        trigger=trigger,
        client_order_id=existing.client_order_id,
        price=existing.price,
        quantity=existing.remaining_quantity,
        reduce_only=existing.reduce_only,
        detail=detail or "cancel the resting quote",
    )


def _replace(plan: SidePlan, existing: Order, *, symbol: str, trigger: QuoteTrigger) -> QuoteDecision:
    return QuoteDecision(
        side=plan.side,
        action=QuoteAction.REPLACE,
        trigger=trigger,
        client_order_id=existing.client_order_id,
        proposal=_proposal(plan, symbol),
        price=plan.desired_price,
        quantity=plan.desired_quantity,
        reduce_only=plan.reduce_only,
        detail=f"replace {existing.client_order_id}: {trigger.value}",
    )


__all__ = ["SidePlan", "plan_side_action"]
