"""RiskGate：所有 OrderProposal 的强制闸门（§10 / §11 / §12 / §13）。

检查顺序（第一个命中即决定 `reason_code`）：

1. **硬检查**（reduce-only 也不能绕过）：
   `SYMBOL_MISMATCH` → `INVALID_ORDER_QUANTITY` → `INVALID_ORDER_PRICE` → `KILL_SWITCH` →
   `BOOK_UNHEALTHY` → `MISSING_MARK_PRICE` → `STALE_MARK`
2. **暴露分类**（INCREASING / REDUCING / REVERSING）
   - `reduce_only` 但实际会增大暴露 → `REDUCE_ONLY_WOULD_INCREASE`
3. **降低暴露的 reduce-only 订单**：硬检查通过即 ALLOW（不受限额约束，§11 的明确要求）
3b. **kill switch REDUCE_ONLY**：只放行真正降低暴露的订单

4. **增加暴露的订单**（含反手）：`POSITION_LIMIT` → `NOTIONAL_LIMIT` → `OPEN_ORDER_EXPOSURE_LIMIT` →
   `INSUFFICIENT_AVAILABLE_BALANCE` → `DAILY_LOSS_LIMIT` → `DRAWDOWN_LIMIT` → `LEVERAGE_LIMIT` →
   `LIQUIDATION_DISTANCE`
   （已配置但数据缺失 → `MISSING_DAILY_PNL` / `MISSING_LIQUIDATION_INFO`，同样 fail closed）

第一版 kill switch **拒绝一切**（含 reduce-only）：§11 把 emergency policy 留待后续定义，
这里采用最保守的读法。
"""

from __future__ import annotations

from risk.limits import RiskLimits
from risk.types import (
    ExposureClass,
    KillSwitchMode,
    OrderProposal,
    RiskDecision,
    RiskDecisionType,
    RiskReasonCode,
    RiskSnapshot,
)


def classify_exposure(proposal: OrderProposal, snapshot: RiskSnapshot) -> ExposureClass:
    """判断订单对暴露的影响方向。"""
    delta = proposal.signed_quantity
    current = snapshot.position_qty

    if current == 0.0:
        return ExposureClass.INCREASING
    if (current > 0.0) == (delta > 0.0):
        return ExposureClass.INCREASING
    if abs(delta) <= abs(current):
        return ExposureClass.REDUCING
    # 反向且数量超过当前持仓：既有平仓也有开仓，gross 暴露增大
    return ExposureClass.REVERSING


class RiskGate:
    """消费 `RiskSnapshot`、输出 `RiskDecision` 的强制闸门。"""

    def __init__(self, limits: RiskLimits) -> None:
        if not isinstance(limits, RiskLimits):
            raise TypeError("limits must be a RiskLimits")
        self._limits = limits

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    def evaluate(
        self,
        proposal: OrderProposal,
        snapshot: RiskSnapshot,
        *,
        book_healthy: bool = True,
    ) -> RiskDecision:
        """评估一个订单提案。"""
        if not isinstance(book_healthy, bool):
            raise TypeError("book_healthy must be a bool")
        applied: list[str] = []

        hard = self._hard_checks(proposal, snapshot, book_healthy=book_healthy, applied=applied)
        if hard is not None:
            return hard

        exposure = classify_exposure(proposal, snapshot)
        applied.append(f"exposure_class:{exposure.value}")

        kill_switch_decision = self._check_reduce_only_mode(proposal, exposure=exposure, applied=applied)
        if kill_switch_decision is not None:
            return kill_switch_decision

        if proposal.reduce_only and exposure is not ExposureClass.REDUCING:
            return self._reject(
                RiskReasonCode.REDUCE_ONLY_WOULD_INCREASE,
                f"reduce_only order would not reduce exposure (class={exposure.value}, "
                f"position_qty={snapshot.position_qty}, delta={proposal.signed_quantity})",
                applied,
            )

        if proposal.reduce_only and exposure is ExposureClass.REDUCING:
            return self._allow_reduction(proposal, snapshot, applied)

        limit_decision = self._increasing_exposure_checks(proposal, snapshot, exposure=exposure, applied=applied)
        if limit_decision is not None:
            return limit_decision

        return self._allow_increase(proposal, snapshot, exposure=exposure, applied=applied)

    @staticmethod
    def _allow_increase(
        proposal: OrderProposal, snapshot: RiskSnapshot, *, exposure: ExposureClass, applied: list[str]
    ) -> RiskDecision:
        return RiskDecision(
            decision=RiskDecisionType.ALLOW,
            reason_code=None,
            details=(
                f"{exposure.value} order within all configured limits "
                f"(notional={proposal.notional}, mark={snapshot.mark_price}, "
                f"equity={snapshot.equity}, available_balance={snapshot.available_balance})"
            ),
            checks_applied=tuple(applied),
        )

    def _check_reduce_only_mode(
        self, proposal: OrderProposal, *, exposure: ExposureClass, applied: list[str]
    ) -> RiskDecision | None:
        if self._limits.effective_kill_switch_mode is not KillSwitchMode.REDUCE_ONLY:
            return None
        if proposal.reduce_only and exposure is ExposureClass.REDUCING:
            return None
        return self._reject(
            RiskReasonCode.KILL_SWITCH,
            "kill switch REDUCE_ONLY: only orders that truly reduce exposure are allowed",
            applied,
        )

    @staticmethod
    def _allow_reduction(
        proposal: OrderProposal, snapshot: RiskSnapshot, applied: list[str]
    ) -> RiskDecision:
        return RiskDecision(
            decision=RiskDecisionType.ALLOW,
            reason_code=None,
            details=(
                f"reduce_only order reduces exposure by {abs(proposal.signed_quantity)} "
                f"(position_qty {snapshot.position_qty} -> "
                f"{snapshot.position_qty + proposal.signed_quantity}); "
                "position / notional / daily-loss / drawdown / leverage / liquidation limits not applied"
            ),
            checks_applied=tuple(applied),
        )

    def _hard_checks(
        self,
        proposal: OrderProposal,
        snapshot: RiskSnapshot,
        *,
        book_healthy: bool,
        applied: list[str],
    ) -> RiskDecision | None:
        parameter_failure = self._check_order_parameters(proposal, snapshot, applied=applied)
        if parameter_failure is not None:
            return parameter_failure
        return self._check_market_data(snapshot, book_healthy=book_healthy, applied=applied)

    def _check_order_parameters(
        self, proposal: OrderProposal, snapshot: RiskSnapshot, *, applied: list[str]
    ) -> RiskDecision | None:
        if proposal.symbol != snapshot.symbol:
            return self._reject(
                RiskReasonCode.SYMBOL_MISMATCH,
                f"proposal symbol {proposal.symbol!r} != snapshot symbol {snapshot.symbol!r}",
                applied,
            )
        if proposal.quantity <= 0.0:
            return self._reject(
                RiskReasonCode.INVALID_ORDER_QUANTITY,
                f"quantity must be > 0, got {proposal.quantity}",
                applied,
            )
        if proposal.price <= 0.0:
            return self._reject(
                RiskReasonCode.INVALID_ORDER_PRICE,
                f"price must be > 0, got {proposal.price}",
                applied,
            )
        applied.append("order_parameters")
        return None

    def _check_market_data(
        self, snapshot: RiskSnapshot, *, book_healthy: bool, applied: list[str]
    ) -> RiskDecision | None:
        mode = self._limits.effective_kill_switch_mode
        if mode is KillSwitchMode.HALT_ALL:
            return self._reject(
                RiskReasonCode.KILL_SWITCH,
                "kill switch HALT_ALL: new submissions are blocked (existing orders can still be canceled)",
                applied,
            )
        if not book_healthy:
            return self._reject(
                RiskReasonCode.BOOK_UNHEALTHY,
                "market data gate: book is not healthy",
                applied,
            )
        if snapshot.mark_price is None:
            return self._reject(
                RiskReasonCode.MISSING_MARK_PRICE,
                "mark price is unknown; PnL / notional cannot be evaluated (fail closed)",
                applied,
            )
        max_age = self._limits.max_mark_age_ms
        if max_age is not None:
            if snapshot.mark_age_ms is None:
                return self._reject(
                    RiskReasonCode.STALE_MARK,
                    f"mark age is unknown while max_mark_age_ms={max_age} is configured (fail closed)",
                    applied,
                )
            if snapshot.mark_age_ms > max_age:
                return self._reject(
                    RiskReasonCode.STALE_MARK,
                    f"mark age {snapshot.mark_age_ms} ms exceeds max_mark_age_ms={max_age}",
                    applied,
                )
            applied.append("mark_age")
        return None


    def _increasing_exposure_checks(
        self,
        proposal: OrderProposal,
        snapshot: RiskSnapshot,
        *,
        exposure: ExposureClass,
        applied: list[str],
    ) -> RiskDecision | None:
        mark = snapshot.mark_price
        assert mark is not None  # 硬检查已保证
        projected_qty = snapshot.position_qty + proposal.signed_quantity
        projected_notional = abs(projected_qty) * mark

        for check in (
            self._check_uncertain_exposure,
            self._check_position_limits,
            self._check_open_order_and_balance,
            self._check_loss_limits,
            self._check_leverage,
            self._check_liquidation_guard,
        ):
            decision = check(
                proposal,
                snapshot,
                projected_qty=projected_qty,
                projected_notional=projected_notional,
                applied=applied,
            )
            if decision is not None:
                return decision
        return None

    def _check_uncertain_exposure(
        self,
        proposal: OrderProposal,
        snapshot: RiskSnapshot,
        *,
        projected_qty: float,  # noqa: ARG002 - 统一签名
        projected_notional: float,  # noqa: ARG002 - 统一签名
        applied: list[str],
    ) -> RiskDecision | None:
        """资料不足的订单暴露无法量化 → 不允许新增暴露（P0001.6.1；reduce-only 降暴露仍放行）。"""
        if snapshot.unresolved_order_count <= 0:
            return None
        applied.append("uncertain_exposure")
        return self._reject(
            RiskReasonCode.UNCERTAIN_EXPOSURE_UNKNOWN,
            f"{snapshot.unresolved_order_count} order(s) have insufficient data to size exposure; "
            "assume they exist until reconciliation confirms otherwise (fail closed)",
            applied,
        )

    def _check_position_limits(
        self,
        proposal: OrderProposal,
        snapshot: RiskSnapshot,
        *,
        projected_qty: float,
        projected_notional: float,
        applied: list[str],
    ) -> RiskDecision | None:
        limits = self._limits

        if limits.max_position_qty is not None:
            applied.append("position_qty")
            if abs(projected_qty) > limits.max_position_qty:
                return self._reject(
                    RiskReasonCode.POSITION_LIMIT,
                    f"projected position {projected_qty} exceeds max_position_qty={limits.max_position_qty}",
                    applied,
                )

        if limits.max_position_notional is not None:
            applied.append("position_notional")
            if projected_notional > limits.max_position_notional:
                return self._reject(
                    RiskReasonCode.NOTIONAL_LIMIT,
                    f"projected notional {projected_notional} exceeds "
                    f"max_position_notional={limits.max_position_notional}",
                    applied,
                )

        return None

    def _check_open_order_and_balance(
        self,
        proposal: OrderProposal,
        snapshot: RiskSnapshot,
        *,
        projected_qty: float,  # noqa: ARG002 - 统一签名
        projected_notional: float,  # noqa: ARG002 - 统一签名
        applied: list[str],
    ) -> RiskDecision | None:
        limits = self._limits

        if limits.max_open_order_exposure is not None:
            applied.append("open_order_exposure")
            projected_open = snapshot.open_order_exposure + proposal.notional
            if projected_open > limits.max_open_order_exposure:
                return self._reject(
                    RiskReasonCode.OPEN_ORDER_EXPOSURE_LIMIT,
                    f"open-order exposure {projected_open} exceeds "
                    f"max_open_order_exposure={limits.max_open_order_exposure}",
                    applied,
                )

        # 可用余额：订单名义价值必须落在 可用余额 × 有效杠杆 之内
        applied.append("available_balance")
        capacity = snapshot.available_balance * limits.effective_leverage
        if proposal.notional > capacity:
            return self._reject(
                RiskReasonCode.INSUFFICIENT_AVAILABLE_BALANCE,
                f"order notional {proposal.notional} exceeds available capacity {capacity} "
                f"(available_balance={snapshot.available_balance}, "
                f"effective_leverage={limits.effective_leverage})",
                applied,
            )

        return None

    def _check_loss_limits(
        self,
        proposal: OrderProposal,
        snapshot: RiskSnapshot,
        *,
        projected_qty: float,  # noqa: ARG002 - 统一签名
        projected_notional: float,  # noqa: ARG002 - 统一签名
        applied: list[str],
    ) -> RiskDecision | None:
        for check in (self._check_daily_loss, self._check_drawdown):
            decision = check(proposal, snapshot, applied=applied)
            if decision is not None:
                return decision
        return None

    def _check_daily_loss(
        self, proposal: OrderProposal, snapshot: RiskSnapshot, *, applied: list[str]
    ) -> RiskDecision | None:
        limit = self._limits.max_daily_loss
        if limit is None:
            return None
        if snapshot.realized_pnl_today is None:
            return self._reject(
                RiskReasonCode.MISSING_DAILY_PNL,
                f"daily PnL is unknown while max_daily_loss={limit} is configured (fail closed)",
                applied,
            )
        applied.append("daily_loss")
        if snapshot.realized_pnl_today <= -limit:
            return self._reject(
                RiskReasonCode.DAILY_LOSS_LIMIT,
                f"today net realized {snapshot.realized_pnl_today} breaches max_daily_loss={limit}",
                applied,
            )
        return None

    def _check_drawdown(
        self, proposal: OrderProposal, snapshot: RiskSnapshot, *, applied: list[str]
    ) -> RiskDecision | None:
        limit = self._limits.max_drawdown_pct
        if limit is None:
            return None
        if snapshot.drawdown_pct is None:
            return self._reject(
                RiskReasonCode.MISSING_MARK_PRICE,
                "drawdown is unknown while max_drawdown_pct is configured (fail closed)",
                applied,
            )
        applied.append("drawdown")
        if snapshot.drawdown_pct >= limit:
            return self._reject(
                RiskReasonCode.DRAWDOWN_LIMIT,
                f"drawdown {snapshot.drawdown_pct:.6f} breaches max_drawdown_pct={limit}",
                applied,
            )
        return None

    def _check_leverage(
        self,
        proposal: OrderProposal,
        snapshot: RiskSnapshot,
        *,
        projected_qty: float,  # noqa: ARG002 - 统一签名
        projected_notional: float,
        applied: list[str],
    ) -> RiskDecision | None:
        limit = self._limits.max_leverage
        if limit is None:
            return None
        equity = snapshot.equity
        if equity is None or equity <= 0.0:
            return self._reject(
                RiskReasonCode.LEVERAGE_LIMIT,
                f"equity {equity} cannot support leverage check (fail closed)",
                applied,
            )
        applied.append("leverage")
        projected_leverage = projected_notional / equity
        if projected_leverage > limit:
            return self._reject(
                RiskReasonCode.LEVERAGE_LIMIT,
                f"projected leverage {projected_leverage} exceeds max_leverage={limit}",
                applied,
            )
        return None

    def _check_liquidation_guard(
        self,
        proposal: OrderProposal,
        snapshot: RiskSnapshot,
        *,
        projected_qty: float,  # noqa: ARG002 - 统一签名
        projected_notional: float,  # noqa: ARG002 - 统一签名
        applied: list[str],
    ) -> RiskDecision | None:
        limits = self._limits
        if limits.min_liquidation_distance_bps is not None:
            liquidation = snapshot.liquidation
            if liquidation is None:
                return self._reject(
                    RiskReasonCode.MISSING_LIQUIDATION_INFO,
                    f"liquidation info is unknown while min_liquidation_distance_bps="
                    f"{limits.min_liquidation_distance_bps} is configured (fail closed)",
                    applied,
                )
            applied.append("liquidation_distance")
            if liquidation.distance_bps < limits.min_liquidation_distance_bps:
                return self._reject(
                    RiskReasonCode.LIQUIDATION_DISTANCE,
                    f"liquidation distance {liquidation.distance_bps} bps is below "
                    f"min_liquidation_distance_bps={limits.min_liquidation_distance_bps} "
                    "(liquidation price comes from the account adapter, never computed here)",
                    applied,
                )

        return None

    @staticmethod
    def _reject(reason: RiskReasonCode, details: str, applied: list[str]) -> RiskDecision:
        return RiskDecision(
            decision=RiskDecisionType.REJECT,
            reason_code=reason,
            details=details,
            checks_applied=tuple(applied),
        )


__all__ = ["RiskGate", "classify_exposure"]
