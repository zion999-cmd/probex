"""Canonical remaining new-exposure budget（**Risk domain owner**，P0001.14 §3）。

本模块把 `RiskGate` 对**增加暴露**订单的既有名义额度检查**只读暴露**为"还剩多少 notional 可增加"，
供 `MakerPolicy.remaining_risk_budget` 与 Runtime/DecisionLoop 消费。

语义与 `RiskGate._increasing_exposure_checks` **完全一致**（不新增、不放宽、不修改任何风险规则）：

    projected_qty       = position_qty + signed_qty
    projected_notional  = |projected_qty| * mark

对**新增暴露**（INCREASING / REVERSING 的开仓部分）的约束：
- `max_position_qty`           ⇒ 剩余 = (limit − |position_qty|) * mark
- `max_position_notional`      ⇒ 剩余 = limit − |position_qty| * mark
- `max_open_order_exposure`    ⇒ 剩余 = limit − snapshot.open_order_exposure（含 pending/uncertain）
- `available_balance`（始终生效）⇒ 剩余 = available_balance * effective_leverage
剩余预算 = 以上**已配置**约束的最小值（负值按 0 = 无额度）。

`mark_price` 未知 ⇒ 无法把数量/名义换算成预算 ⇒ 返回 `None`（fail closed，调用方不得当作 0 或无穷）。
"""

from __future__ import annotations

from dataclasses import dataclass

from risk.limits import RiskLimits
from risk.types import RiskSnapshot


class BudgetError(ValueError):
    """risk budget 契约错误。"""


@dataclass(frozen=True, slots=True)
class ExposureBudget:
    """剩余新增暴露预算（`None` = 未知 ⇒ fail closed）。"""

    remaining_notional: float | None
    #: 每个已生效约束的剩余额度（审计用）
    constraints: tuple[tuple[str, float], ...] = ()
    #: 导致未知的缺失事实
    missing: tuple[str, ...] = ()
    #: 预算为 0 的原因（例如已有额度被 pending exposure 占满）
    note: str = ""


def remaining_exposure_budget(snapshot: RiskSnapshot, limits: RiskLimits) -> ExposureBudget:
    """由现有 Risk domain 事实计算 canonical 剩余新增暴露预算。"""
    if not isinstance(snapshot, RiskSnapshot):
        raise BudgetError("remaining_exposure_budget requires a RiskSnapshot")
    if not isinstance(limits, RiskLimits):
        raise BudgetError("remaining_exposure_budget requires RiskLimits")

    mark = snapshot.mark_price
    if mark is None or mark <= 0:
        return ExposureBudget(remaining_notional=None, missing=("mark_price",),
                              note="mark price is unknown; notional headroom cannot be derived (fail closed)")
    if snapshot.unresolved_order_count > 0:
        # 与 gate 一致：暴露无法量化的订单存在 ⇒ 不允许任何新增暴露（不是未知，而是额度为 0）
        return ExposureBudget(
            remaining_notional=0.0,
            missing=(),
            note=(f"{snapshot.unresolved_order_count} order(s) have unquantified exposure; "
                  "the gate blocks all new exposure until reconciliation confirms otherwise"))

    position_qty = float(snapshot.position_qty)
    position_notional = abs(position_qty) * float(mark)
    candidates: list[tuple[str, float]] = []
    if limits.max_position_qty is not None:
        candidates.append(("max_position_qty",
                           (float(limits.max_position_qty) - abs(position_qty)) * float(mark)))
    if limits.max_position_notional is not None:
        candidates.append(("max_position_notional",
                           float(limits.max_position_notional) - position_notional))
    if limits.max_open_order_exposure is not None:
        candidates.append(("max_open_order_exposure",
                           float(limits.max_open_order_exposure) - float(snapshot.open_order_exposure)))
    # available_balance 约束始终生效（与 gate 一致：order notional 必须落在 available_balance × leverage 内）
    candidates.append(("available_balance",
                       float(snapshot.available_balance) * float(limits.effective_leverage)))

    remaining = max(0.0, min(value for _, value in candidates))
    note = "" if remaining > 0.0 else "no remaining new-exposure budget under the configured limits"
    return ExposureBudget(remaining_notional=remaining,
                          constraints=tuple((name, float(value)) for name, value in candidates),
                          note=note)


__all__ = ["BudgetError", "ExposureBudget", "remaining_exposure_budget"]
