"""Order / OrderStatus：不变量与状态转换表。"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from execution.types import (
    ALLOWED_TRANSITIONS,
    LIMIT_ORDER_TYPE,
    IllegalOrderTransition,
    InvalidOrderError,
    Order,
    OrderStatus,
    can_transition,
)
from market.events.types import Venue
from portfolio.types import Side


def _order(**overrides: object) -> Order:
    values: dict[str, object] = {
        "client_order_id": "probex-s1-000001",
        "venue": Venue.BINANCE,
        "symbol": "BTCUSDT",
        "side": Side.BUY,
        "price": 100.0,
        "quantity": 1.0,
        "status": OrderStatus.PENDING_CREATE,
        "created_at": 1_000,
        "updated_at": 1_000,
    }
    values.update(overrides)
    return Order(**values)  # type: ignore[arg-type]


class OrderStatusTest(unittest.TestCase):
    def test_status_vocabulary_is_fixed(self) -> None:
        self.assertEqual(
            [status.value for status in OrderStatus],
            [
                "PENDING_CREATE",
                "OPEN",
                "PARTIALLY_FILLED",
                "PENDING_CANCEL",
                "FILLED",
                "CANCELED",
                "FAILED",
                "EXPIRED",
                "LOST",
            ],
        )

    def test_terminal_and_active_classification(self) -> None:
        terminal = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.FAILED, OrderStatus.EXPIRED}
        for status in OrderStatus:
            with self.subTest(status=status.value):
                self.assertEqual(status.is_terminal, status in terminal)
                self.assertEqual(status.is_lost, status is OrderStatus.LOST)
                if status is OrderStatus.LOST:
                    self.assertFalse(status.is_terminal, "LOST 不是事实终态")
                    self.assertFalse(status.is_active, "LOST 有自己的视图")

    def test_lost_can_be_restored_but_terminal_cannot(self) -> None:
        for target in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELED):
            with self.subTest(target=target.value):
                self.assertTrue(can_transition(OrderStatus.LOST, target))
        for source in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.FAILED, OrderStatus.EXPIRED):
            with self.subTest(source=source.value):
                self.assertEqual(ALLOWED_TRANSITIONS[source], frozenset())

    def test_cancel_race_transitions_are_legal(self) -> None:
        self.assertTrue(can_transition(OrderStatus.OPEN, OrderStatus.PENDING_CANCEL))
        self.assertTrue(can_transition(OrderStatus.PENDING_CANCEL, OrderStatus.PARTIALLY_FILLED))
        self.assertTrue(can_transition(OrderStatus.PENDING_CANCEL, OrderStatus.FILLED))
        self.assertTrue(can_transition(OrderStatus.PENDING_CANCEL, OrderStatus.CANCELED))
        self.assertTrue(can_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING_CANCEL))

    def test_illegal_transitions(self) -> None:
        self.assertFalse(can_transition(OrderStatus.CANCELED, OrderStatus.OPEN))
        self.assertFalse(can_transition(OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED))
        self.assertFalse(can_transition(OrderStatus.PENDING_CREATE, OrderStatus.PARTIALLY_FILLED))
        self.assertFalse(can_transition(OrderStatus.OPEN, OrderStatus.OPEN))


class OrderTest(unittest.TestCase):
    def test_type_is_limit_only_in_v1(self) -> None:
        self.assertEqual(LIMIT_ORDER_TYPE, "limit")
        self.assertEqual(_order().order_type, "limit")
        with self.assertRaises(InvalidOrderError):
            _order(order_type="market")

    def test_order_is_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            _order().price = 1.0  # type: ignore[misc]

    def test_derived_fields(self) -> None:
        order = _order(filled_quantity=0.4, avg_fill_price=100.0)
        self.assertAlmostEqual(order.remaining_quantity, 0.6)
        self.assertAlmostEqual(order.notional, 60.0)  # pending exposure 按订单价格

    def test_executed_quantity_tracks_terminal_state(self) -> None:
        open_order = _order(status=OrderStatus.OPEN, filled_quantity=0.4, avg_fill_price=100.0)
        self.assertEqual(open_order.executed_quantity, 0.4)

        canceled = open_order.with_status(OrderStatus.CANCELED, timestamp=2_000)
        self.assertEqual(canceled.final_executed_quantity, 0.4)
        self.assertEqual(canceled.executed_quantity, 0.4)

    def test_invalid_orders_rejected(self) -> None:
        cases = (
            {"client_order_id": ""},
            {"symbol": ""},
            {"price": 0.0},
            {"quantity": 0.0},
            {"filled_quantity": 1.5},
            {"filled_quantity": 0.5},  # 有成交却没有均价
            {"created_at": -1},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(InvalidOrderError):
                    _order(**overrides)

    def test_view_returns_lifecycle_fields(self) -> None:
        view = _order(exchange_order_id="paper-1").view()
        for key in (
            "client_order_id",
            "exchange_order_id",
            "venue",
            "symbol",
            "side",
            "type",
            "price",
            "quantity",
            "filled_quantity",
            "avg_fill_price",
            "reduce_only",
            "post_only",
            "status",
            "created_at",
            "updated_at",
        ):
            with self.subTest(key=key):
                self.assertIn(key, view)

    def test_illegal_transition_error_is_execution_error(self) -> None:
        from execution.types import ExecutionError

        self.assertTrue(issubclass(IllegalOrderTransition, ExecutionError))


if __name__ == "__main__":
    unittest.main()
