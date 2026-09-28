"""P0001.7 单元测试：QuoteLifecycle（SC-9 KEEP / CANCEL / REPLACE 与触发优先级）。"""

from __future__ import annotations

import unittest

from execution.types import Order, OrderStatus
from market.events.types import Venue
from portfolio.types import Side
from strategy.maker.lifecycle import SidePlan, plan_side_action
from strategy.maker.types import QuoteAction, QuoteTrigger
from tests.strategy_support import maker_config
from tests.support import BASE_TS, SYMBOL


def _order(
    *,
    side: Side = Side.BUY,
    price: float = 100.0,
    quantity: float = 1.0,
    filled: float = 0.0,
    status: OrderStatus = OrderStatus.OPEN,
    created_at: int = BASE_TS,
    reduce_only: bool = False,
) -> Order:
    return Order(
        client_order_id="probex-s1-000001",
        venue=Venue.BINANCE,
        symbol=SYMBOL,
        side=side,
        price=price,
        quantity=quantity,
        status=status,
        created_at=created_at,
        updated_at=created_at,
        filled_quantity=filled,
        avg_fill_price=price if filled > 0.0 else 0.0,
        reduce_only=reduce_only,
        post_only=True,
    )


def _plan(
    *,
    side: Side = Side.BUY,
    price: float | None = 100.0,
    quantity: float | None = 1.0,
    reduce_only: bool = False,
    forbidden: QuoteTrigger | None = None,
    detail: str = "blocked",
) -> SidePlan:
    return SidePlan(
        side=side,
        desired_price=price,
        desired_quantity=quantity,
        reduce_only=reduce_only,
        forbidden=forbidden,
        forbidden_detail=detail,
    )


def _action(plan: SidePlan, existing: Order | None = None, *, now_ms: int = BASE_TS, **kwargs):
    return plan_side_action(
        plan=plan,
        existing=existing,
        symbol=SYMBOL,
        config=maker_config(),
        now_ms=now_ms,
        **kwargs,
    )


class QuoteLifecycleTest(unittest.TestCase):
    def test_places_when_nothing_is_resting(self) -> None:
        decision = _action(_plan())

        self.assertIs(decision.action, QuoteAction.PLACE)
        self.assertIs(decision.trigger, QuoteTrigger.INITIAL_QUOTE)
        self.assertTrue(decision.proposal.post_only)
        self.assertEqual(decision.proposal.quantity, 1.0)

    def test_none_when_no_quote_is_desired(self) -> None:
        decision = _action(_plan(price=None, quantity=None))

        self.assertIs(decision.action, QuoteAction.NONE)
        self.assertIsNone(decision.proposal)

    def test_keeps_a_quote_within_tolerance(self) -> None:
        decision = _action(_plan(), _order())

        self.assertIs(decision.action, QuoteAction.KEEP)
        self.assertIs(decision.trigger, QuoteTrigger.WITHIN_TOLERANCE)

    def test_replaces_when_price_moved_enough_ticks(self) -> None:
        # 2 ticks × 0.01 = 0.02 触发（price_move_ticks_replace = 2）
        decision = _action(_plan(price=99.98), _order(price=100.0))

        self.assertIs(decision.action, QuoteAction.REPLACE)
        self.assertIs(decision.trigger, QuoteTrigger.PRICE_MOVED)
        self.assertEqual(decision.client_order_id, "probex-s1-000001")
        self.assertAlmostEqual(decision.proposal.price, 99.98)

    def test_price_move_below_threshold_keeps(self) -> None:
        decision = _action(_plan(price=99.99), _order(price=100.0))

        self.assertIs(decision.action, QuoteAction.KEEP)

    def test_replaces_when_quote_age_exceeded(self) -> None:
        decision = _action(_plan(), _order(created_at=BASE_TS - 5_001), now_ms=BASE_TS)

        self.assertIs(decision.action, QuoteAction.REPLACE)
        self.assertIs(decision.trigger, QuoteTrigger.QUOTE_AGE_EXCEEDED)

    def test_quote_age_boundary_keeps(self) -> None:
        decision = _action(_plan(), _order(created_at=BASE_TS - 5_000), now_ms=BASE_TS)

        self.assertIs(decision.action, QuoteAction.KEEP)

    def test_replaces_when_size_drifted(self) -> None:
        decision = _action(_plan(quantity=0.4), _order(quantity=1.0))

        self.assertIs(decision.action, QuoteAction.REPLACE)
        self.assertIs(decision.trigger, QuoteTrigger.SIZE_DRIFT)

    def test_replaces_when_reduce_only_semantics_changed(self) -> None:
        decision = _action(_plan(reduce_only=True), _order(reduce_only=False))

        self.assertIs(decision.action, QuoteAction.REPLACE)
        self.assertIs(decision.trigger, QuoteTrigger.SIZE_DRIFT)

    def test_replaces_on_partial_fill_remaining_drift(self) -> None:
        decision = _action(_plan(quantity=1.0), _order(quantity=1.0, filled=0.5))

        self.assertIs(decision.action, QuoteAction.REPLACE)

    def test_cancels_all_when_halted(self) -> None:
        decision = _action(
            _plan(price=None, quantity=None, forbidden=QuoteTrigger.KILL_SWITCH),
            _order(reduce_only=True),
            cancel_all=True,
            gate_trigger=QuoteTrigger.KILL_SWITCH,
        )

        self.assertIs(decision.action, QuoteAction.CANCEL)
        self.assertIs(decision.trigger, QuoteTrigger.KILL_SWITCH)

    def test_cancels_increasing_quotes_under_a_global_block(self) -> None:
        decision = _action(
            _plan(price=None, quantity=None, forbidden=QuoteTrigger.PREDICTION_STALE),
            _order(reduce_only=False),
            cancel_increasing=True,
            gate_trigger=QuoteTrigger.PREDICTION_STALE,
        )

        self.assertIs(decision.action, QuoteAction.CANCEL)
        self.assertIs(decision.trigger, QuoteTrigger.PREDICTION_STALE)

    def test_keeps_reduce_only_quotes_under_a_global_block(self) -> None:
        decision = _action(
            _plan(price=None, quantity=None, forbidden=QuoteTrigger.PREDICTION_STALE),
            _order(reduce_only=True),
            cancel_increasing=True,
            gate_trigger=QuoteTrigger.PREDICTION_STALE,
        )

        self.assertIs(decision.action, QuoteAction.KEEP)
        self.assertIs(decision.trigger, QuoteTrigger.KEEP_REDUCE_ONLY)

    def test_global_block_cancels_before_price_comparison(self) -> None:
        decision = _action(
            _plan(forbidden=QuoteTrigger.MARKET_UNHEALTHY),
            _order(price=99.0),
            cancel_increasing=True,
            gate_trigger=QuoteTrigger.MARKET_UNHEALTHY,
        )

        self.assertIs(decision.action, QuoteAction.CANCEL)

    def test_side_specific_forbid_cancels_the_side(self) -> None:
        decision = _action(
            _plan(price=None, quantity=None, forbidden=QuoteTrigger.ADVERSE_SELECTION),
            _order(side=Side.SELL, reduce_only=False),
        )

        self.assertIs(decision.action, QuoteAction.CANCEL)
        self.assertIs(decision.trigger, QuoteTrigger.ADVERSE_SELECTION)

    def test_side_specific_forbid_keeps_a_reduce_only_quote(self) -> None:
        decision = _action(
            _plan(side=Side.SELL, price=None, quantity=None, forbidden=QuoteTrigger.ADVERSE_SELECTION),
            _order(side=Side.SELL, reduce_only=True),
        )

        self.assertIs(decision.action, QuoteAction.KEEP)
        self.assertIs(decision.trigger, QuoteTrigger.KEEP_REDUCE_ONLY)

    def test_lost_order_is_never_replaced_or_canceled(self) -> None:
        decision = _action(_plan(), _order(status=OrderStatus.LOST))

        self.assertIs(decision.action, QuoteAction.KEEP)
        self.assertIs(decision.trigger, QuoteTrigger.UNKNOWN_ORDER_STATE)

    def test_pending_cancel_is_not_canceled_again(self) -> None:
        decision = _action(_plan(), _order(status=OrderStatus.PENDING_CANCEL))

        self.assertIs(decision.action, QuoteAction.KEEP)
        self.assertIs(decision.trigger, QuoteTrigger.CANCEL_IN_FLIGHT)

    def test_terminal_order_is_replaced_by_a_fresh_placement(self) -> None:
        decision = _action(_plan(), _order(status=OrderStatus.CANCELED))

        self.assertIs(decision.action, QuoteAction.PLACE)
        self.assertIsNone(decision.client_order_id)

    def test_invalid_decision_combinations_are_rejected(self) -> None:
        from risk.types import OrderProposal
        from strategy.maker.types import QuoteDecision

        non_post_only = OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=1.0, price=100.0, post_only=False)
        post_only = OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=1.0, price=100.0, post_only=True)

        with self.assertRaises(ValueError):  # PLACE 必须带 proposal
            QuoteDecision(side=Side.BUY, action=QuoteAction.PLACE, trigger=QuoteTrigger.INITIAL_QUOTE)
        with self.assertRaises(ValueError):  # KEEP 必须带 client_order_id
            QuoteDecision(side=Side.BUY, action=QuoteAction.KEEP, trigger=QuoteTrigger.WITHIN_TOLERANCE)
        with self.assertRaises(ValueError):  # 非 post-only 报价不允许
            QuoteDecision(
                side=Side.BUY,
                action=QuoteAction.PLACE,
                trigger=QuoteTrigger.INITIAL_QUOTE,
                proposal=non_post_only,
            )
        with self.assertRaises(ValueError):  # KEEP 不允许携带 proposal
            QuoteDecision(
                side=Side.BUY,
                action=QuoteAction.KEEP,
                trigger=QuoteTrigger.WITHIN_TOLERANCE,
                client_order_id="probex-s1-000001",
                proposal=post_only,
            )
        with self.assertRaises(ValueError):  # NONE 不允许携带任何订单
            QuoteDecision(
                side=Side.BUY,
                action=QuoteAction.NONE,
                trigger=QuoteTrigger.SIDE_NOT_PERMITTED,
                client_order_id="probex-s1-000001",
            )


if __name__ == "__main__":
    unittest.main()
