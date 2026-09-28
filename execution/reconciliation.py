"""Reconciliation：让本地订单状态与外部事实收敛（P0001.6 §11）。

契约：

```text
reconcile(tracker, external_open_orders, external_recent_fills, timestamp)
    → (ReconciliationReport, canonical_fills)
```

覆盖情形：

1. 外部有、本地已有 → 应用外部 fill（去重后）并校正成交量 / 状态；
2. 外部有、本地未知 → **adopt**（需要外部给出 quantity，否则 fail closed）；
3. 本地 active、外部不存在 → 标记 `LOST`；
4. 本地 `LOST`、外部存在 → 恢复为外部真实状态；
5. 本地 filled 落后于外部 → 校正（`QUANTITY_CORRECTED`）。

`converged` 表示**不需要任何纠正动作**（仅补记 fill 不算纠正）。因此「reconcile → 再 reconcile」应当收敛。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from execution.events import FillReceived, OrderStatusUpdate
from execution.tracker import OrderTracker, TrackerUpdate
from execution.types import (
    ExternalFill,
    ExternalOrder,
    IllegalOrderTransition,
    Order,
    OrderStatus,
)
from market.events.types import Milliseconds, Venue
from portfolio.types import Fill, Side


class ReconciliationActionKind(Enum):
    FILL_APPLIED = "fill_applied"
    MARKED_LOST = "marked_lost"
    RESTORED = "restored"
    ADOPTED = "adopted"
    QUANTITY_CORRECTED = "quantity_corrected"
    STATUS_CORRECTED = "status_corrected"
    #: 本地已终态、外部仍报非终态 → 冲突，不擅自回退（§7）
    STATUS_CONFLICT = "status_conflict"
    ADOPT_REJECTED = "adopt_rejected"


#: 需要「纠正」的动作（用于判定是否收敛）。
_CORRECTIVE_KINDS = frozenset(
    {
        ReconciliationActionKind.MARKED_LOST,
        ReconciliationActionKind.RESTORED,
        ReconciliationActionKind.ADOPTED,
        ReconciliationActionKind.QUANTITY_CORRECTED,
        ReconciliationActionKind.STATUS_CORRECTED,
        ReconciliationActionKind.STATUS_CONFLICT,
        ReconciliationActionKind.ADOPT_REJECTED,
    }
)


@dataclass(frozen=True, slots=True)
class ReconciliationAction:
    kind: ReconciliationActionKind
    client_order_id: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    actions: tuple[ReconciliationAction, ...]
    canonical_fills: tuple[Fill, ...]

    @property
    def corrective_actions(self) -> tuple[ReconciliationAction, ...]:
        return tuple(action for action in self.actions if action.kind in _CORRECTIVE_KINDS)

    @property
    def converged(self) -> bool:
        """不需要任何纠正动作即视为收敛。"""
        return not self.corrective_actions

    def kinds(self) -> tuple[ReconciliationActionKind, ...]:
        return tuple(action.kind for action in self.actions)


def reconcile(
    tracker: OrderTracker,
    *,
    external_open_orders: tuple[ExternalOrder, ...] = (),
    external_recent_fills: tuple[ExternalFill, ...] = (),
    external_history: tuple[ExternalOrder, ...] = (),
    timestamp: Milliseconds,
    venue: Venue | None = None,
) -> ReconciliationReport:
    """把外部事实并入本地 tracker。返回 report 与需要记账的 canonical fills。

    `external_history`（P0001.9.3 新增，可选）：交易所**历史订单**里**终态**的证据，
    用于解决「本地 active/LOST，但交易所已不在挂单列表」——若历史明确是终态，按终态收敛而不是判 LOST。
    调用方应传入每个 `client_order_id` 的**最新**一条（见 `latest_by_client_order_id`）。
    """
    actions, fills = _apply_external_fills(tracker, external_recent_fills)

    external_by_id = {external.client_order_id: external for external in external_open_orders}
    history_by_id = {external.client_order_id: external for external in external_history}
    actions.extend(
        _reconcile_local_orders(tracker, external_by_id, history_by_id=history_by_id, timestamp=timestamp)
    )

    known = {order.client_order_id for order in tracker.orders}
    tracker_venue = venue or tracker.venue
    for external in external_open_orders:
        if external.client_order_id in known:
            continue
        actions.append(_adopt(tracker, external, timestamp=timestamp, venue=tracker_venue))

    return ReconciliationReport(actions=tuple(actions), canonical_fills=tuple(fills))


def _apply_external_fills(
    tracker: OrderTracker, external_recent_fills: tuple[ExternalFill, ...]
) -> tuple[list[ReconciliationAction], list[Fill]]:
    """先应用外部成交（execution 层去重 + accounting 层再兜底）。"""
    actions: list[ReconciliationAction] = []
    fills: list[Fill] = []
    for external_fill in external_recent_fills:
        update = tracker.on_event(
            FillReceived(
                client_order_id=external_fill.client_order_id,
                execution_id=external_fill.execution_id,
                trade_id=external_fill.trade_id,
                price=external_fill.price,
                quantity=external_fill.quantity,
                timestamp=external_fill.timestamp,
                fee=external_fill.fee,
                fee_asset=external_fill.fee_asset,
            )
        )
        if update.needs_accounting and update.fill is not None:
            fills.append(update.fill)
        actions.append(
            ReconciliationAction(
                ReconciliationActionKind.FILL_APPLIED, external_fill.client_order_id, update.outcome.value
            )
        )
    return actions, fills


def _reconcile_local_orders(
    tracker: OrderTracker,
    external_by_id: dict[str, ExternalOrder],
    *,
    history_by_id: dict[str, ExternalOrder] | None = None,
    timestamp: Milliseconds,
) -> list[ReconciliationAction]:
    """本地 active / LOST 与外部事实对齐（挂单列表优先，其次历史终态证据）。"""
    actions: list[ReconciliationAction] = []
    history = history_by_id if history_by_id is not None else {}
    for order in tracker.orders:
        external = external_by_id.get(order.client_order_id)
        if external is None:
            terminal = history.get(order.client_order_id)
            if terminal is not None and terminal.status.is_terminal:
                if order.status.is_terminal and order.status is not terminal.status:
                    # 两侧都已是终态却不一致（例如本地 CANCELED vs 交易所 FILLED）⇒ 事实自相矛盾，
                    # 不擅自回退本地终态，也不静默接受（未知成交仍由 external_recent_fills 表达）
                    actions.append(
                        ReconciliationAction(
                            ReconciliationActionKind.STATUS_CONFLICT,
                            order.client_order_id,
                            f"local {order.status.value} vs history {terminal.status.value}",
                        )
                    )
                    continue
                if order.status.is_terminal:
                    # 两侧终态一致 ⇒ 已收敛
                    continue
                # 交易所历史明确给出终态 ⇒ 收敛到终态（不判 LOST）
                _apply_status(tracker, order, terminal, timestamp=timestamp)
                actions.append(
                    ReconciliationAction(
                        ReconciliationActionKind.STATUS_CORRECTED,
                        order.client_order_id,
                        f"history_terminal:{terminal.status.value}",
                    )
                )
                tracker.clear_unresolved_order(order.client_order_id)
                continue
            if order.is_active:
                tracker.mark_lost(
                    order.client_order_id,
                    timestamp=timestamp,
                    reason="reconciliation: local active order missing externally",
                )
                actions.append(
                    ReconciliationAction(
                        ReconciliationActionKind.MARKED_LOST, order.client_order_id, order.status.value
                    )
                )
            continue

        if order.is_lost:
            _apply_status(tracker, order, external, timestamp=timestamp)
            tracker.clear_unresolved_order(order.client_order_id)
            actions.append(
                ReconciliationAction(
                    ReconciliationActionKind.RESTORED, order.client_order_id, external.status.value
                )
            )
            continue

        correction = _correct_order(tracker, order, external, timestamp=timestamp)
        if correction is not None:
            actions.append(correction)
        if not correction or correction.kind is not ReconciliationActionKind.ADOPT_REJECTED:
            # 外部事实已确认（终态或一致）→ 释放该订单的「资料不足」标记
            tracker.clear_unresolved_order(order.client_order_id)
    return actions


def _correct_order(
    tracker: OrderTracker, order: Order, external: ExternalOrder, *, timestamp: Milliseconds
) -> ReconciliationAction | None:
    if order.status.is_terminal and not external.status.is_terminal:
        # §7：终态不得被「外部仍报活」回退 —— 如实报告冲突，交给 operator
        return ReconciliationAction(
            ReconciliationActionKind.STATUS_CONFLICT,
            order.client_order_id,
            f"local {order.status.value} vs external {external.status.value}",
        )

    if external.filled_quantity > order.filled_quantity + 1e-9:
        if external.avg_fill_price <= 0.0:
            # 有成交却没有均价 → 无法安全校正（fail closed）
            return ReconciliationAction(
                ReconciliationActionKind.QUANTITY_CORRECTED,
                order.client_order_id,
                "skipped: external order reports filled quantity without avg_fill_price (fail closed)",
            )
        tracker.on_event(
            OrderStatusUpdate(
                client_order_id=order.client_order_id,
                status=external.status if external.status is not order.status else order.status,
                timestamp=timestamp,
                detail="reconciliation: quantity correction",
                exchange_order_id=external.exchange_order_id,
                filled_quantity=external.filled_quantity,
                avg_fill_price=external.avg_fill_price,
            )
        )
        return ReconciliationAction(
            ReconciliationActionKind.QUANTITY_CORRECTED,
            order.client_order_id,
            f"{order.filled_quantity} -> {external.filled_quantity}",
        )
    if external.status is not order.status:
        _apply_status(tracker, order, external, timestamp=timestamp)
        return ReconciliationAction(
            ReconciliationActionKind.STATUS_CORRECTED, order.client_order_id, external.status.value
        )
    return None


def _apply_status(
    tracker: OrderTracker, order: Order, external: ExternalOrder, *, timestamp: Milliseconds
) -> None:
    tracker.on_event(
        OrderStatusUpdate(
            client_order_id=order.client_order_id,
            status=external.status,
            timestamp=timestamp,
            detail="reconciliation: external status",
            exchange_order_id=external.exchange_order_id,
            filled_quantity=external.filled_quantity,
            # 外部已部分成交时，状态更新必须同时带上均价，否则 filled>0 与 avg=0 的组合会被
            # Order 契约判定为非法（P0001.9.3 恢复路径暴露出的缺口）
            avg_fill_price=external.avg_fill_price,
        )
    )


def _adopt(
    tracker: OrderTracker, external: ExternalOrder, *, timestamp: Milliseconds, venue: Venue
) -> ReconciliationAction:
    blocker = _adopt_blocker(external)
    if blocker is not None:
        # P0001.6.1：资料不足时**不按 0 处理**，而是记录为不确定暴露 → Risk 必须 fail closed
        tracker.note_unresolved_order(external.client_order_id, reason=blocker)
        return ReconciliationAction(ReconciliationActionKind.ADOPT_REJECTED, external.client_order_id, blocker)
    try:
        tracker.register(
            Order(
                client_order_id=external.client_order_id,
                venue=venue,
                symbol=external.symbol,
                side=external.side,
                price=external.price,
                quantity=external.quantity,
                status=external.status,
                created_at=timestamp,
                updated_at=timestamp,
                exchange_order_id=external.exchange_order_id,
                filled_quantity=external.filled_quantity,
                avg_fill_price=external.avg_fill_price,
            )
        )
    except IllegalOrderTransition as exc:  # 已存在 / 数据非法 → 如实报告
        tracker.note_unresolved_order(external.client_order_id, reason=str(exc))
        return ReconciliationAction(
            ReconciliationActionKind.ADOPT_REJECTED, external.client_order_id, str(exc)
        )
    tracker.clear_unresolved_order(external.client_order_id)
    return ReconciliationAction(
        ReconciliationActionKind.ADOPTED, external.client_order_id, external.status.value
    )


def _adopt_blocker(external: ExternalOrder) -> str | None:
    """返回无法 adopt 的原因；None 表示资料齐备。"""
    if external.filled_quantity > 0.0 and external.avg_fill_price <= 0.0:
        return "external order has fills but no avg_fill_price; cannot adopt safely (fail closed)"
    if external.side is None or external.price is None or external.price <= 0.0:
        return "external order has no usable side/price; cannot adopt safely (fail closed)"
    if external.quantity is None or external.quantity <= 0.0:
        return "external order has no quantity; cannot adopt safely (fail closed)"
    return None


__all__ = [
    "ReconciliationAction",
    "ReconciliationActionKind",
    "ReconciliationReport",
    "reconcile",
]
