"""P0001.14 §3：canonical remaining exposure budget 必须与 RiskGate 语义一致（不新增风险规则）。"""

from __future__ import annotations

import unittest

from portfolio.types import Side
from risk.budget import BudgetError, remaining_exposure_budget
from risk.gate import RiskGate
from risk.limits import RiskLimits
from risk.types import OrderProposal, RiskSnapshot


def snapshot(*, mark_price=100.0, position_qty=0.0, open_order_exposure=0.0,
             available_balance=1_000.0, unresolved_order_count=0) -> RiskSnapshot:
    return RiskSnapshot(
        symbol="BTCUSDT", now_ms=1_000, balance=available_balance + open_order_exposure,
        equity=available_balance + open_order_exposure, position_qty=position_qty,
        position_notional=abs(position_qty) * (mark_price or 0.0), gross_exposure=None,
        net_exposure=None, open_order_exposure=open_order_exposure,
        available_balance=available_balance, realized_pnl_today=None, unrealized_pnl=None,
        drawdown=None, mark_price=mark_price, unresolved_order_count=unresolved_order_count)


class ExposureBudgetTest(unittest.TestCase):
    def test_unknown_mark_price_is_unknown_not_zero(self) -> None:
        budget = remaining_exposure_budget(snapshot(mark_price=None), RiskLimits(max_position_notional=100.0))

        self.assertIsNone(budget.remaining_notional)       # fail closed；不得当作 0/无穷
        self.assertEqual(budget.missing, ("mark_price",))
        self.assertIn("fail closed", budget.note)

    def test_notional_limit_headroom_subtracts_current_position(self) -> None:
        budget = remaining_exposure_budget(snapshot(position_qty=0.25),
                                           RiskLimits(max_position_notional=100.0))

        self.assertAlmostEqual(budget.remaining_notional, 75.0)   # 100 - 0.25*100

    def test_qty_limit_headroom_is_converted_with_mark_price(self) -> None:
        budget = remaining_exposure_budget(snapshot(position_qty=0.25),
                                           RiskLimits(max_position_qty=1.0))

        self.assertAlmostEqual(budget.remaining_notional, 75.0)   # (1 - 0.25) * 100

    def test_open_order_exposure_headroom_includes_pending(self) -> None:
        budget = remaining_exposure_budget(snapshot(open_order_exposure=40.0),
                                           RiskLimits(max_open_order_exposure=100.0))

        self.assertAlmostEqual(budget.remaining_notional, 60.0)

    def test_budget_is_the_minimum_of_the_configured_constraints(self) -> None:
        budget = remaining_exposure_budget(
            snapshot(position_qty=0.25, open_order_exposure=40.0, available_balance=0.5),
            RiskLimits(max_position_notional=100.0, max_position_qty=1.0, max_open_order_exposure=100.0))

        # qty ⇒ 75 | notional ⇒ 75 | open order ⇒ 60 | available_balance ⇒ 0.5 × 1.0
        self.assertAlmostEqual(budget.remaining_notional, 0.5)
        self.assertEqual({name for name, _ in budget.constraints},
                         {"max_position_qty", "max_position_notional", "max_open_order_exposure",
                          "available_balance"})

    def test_available_balance_uses_effective_leverage_like_the_gate(self) -> None:
        budget = remaining_exposure_budget(snapshot(available_balance=10.0),
                                           RiskLimits(max_leverage=3.0))

        self.assertAlmostEqual(budget.remaining_notional, 30.0)

    def test_without_configured_limits_only_balance_capacity_applies(self) -> None:
        budget = remaining_exposure_budget(snapshot(available_balance=7.5), RiskLimits())

        self.assertAlmostEqual(budget.remaining_notional, 7.5)

    def test_exhausted_headroom_is_zero_and_explained(self) -> None:
        budget = remaining_exposure_budget(snapshot(position_qty=1.0),
                                           RiskLimits(max_position_notional=100.0))

        self.assertEqual(budget.remaining_notional, 0.0)
        self.assertIn("no remaining", budget.note)

    def test_unquantified_orders_leave_no_new_exposure_budget(self) -> None:
        budget = remaining_exposure_budget(snapshot(unresolved_order_count=2),
                                           RiskLimits(max_position_notional=100.0))

        self.assertEqual(budget.remaining_notional, 0.0)
        self.assertIn("unquantified exposure", budget.note)

    def test_budget_agrees_with_the_gate_on_whether_an_order_fits(self) -> None:
        limits = RiskLimits(max_position_notional=100.0, max_open_order_exposure=200.0)
        fresh = snapshot(available_balance=100.0)
        gate = RiskGate(limits)
        budget = remaining_exposure_budget(fresh, limits)

        for notional in (50.0, 99.999, 100.0, 100.001, 250.0):
            proposal = OrderProposal(symbol="BTCUSDT", side=Side.BUY, quantity=notional / 100.0,
                                     price=100.0, post_only=True)
            with self.subTest(notional=notional):
                self.assertEqual(gate.evaluate(proposal, fresh).allowed,
                                 proposal.notional <= budget.remaining_notional)

    def test_rejects_wrong_input_types(self) -> None:
        with self.assertRaises(BudgetError):
            remaining_exposure_budget(object(), RiskLimits())
        with self.assertRaises(BudgetError):
            remaining_exposure_budget(snapshot(), object())


if __name__ == "__main__":
    unittest.main()
