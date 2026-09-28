"""Fault：重复成交不得重复记账（SC-2）。"""

from __future__ import annotations

import unittest

from portfolio.accounting import AccountingCore
from portfolio.fills import FillOutcome
from portfolio.types import Side
from risk.gate import RiskGate
from risk.limits import RiskLimits
from risk.snapshot import build_risk_snapshot
from risk.types import OrderProposal, RiskDecisionType
from tests.support import BASE_TS, SYMBOL, make_fill


class DuplicateFillAccountingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.core = AccountingCore(initial_balance=1_000.0)

    def test_sc2_duplicate_fill_id_is_not_counted_twice(self) -> None:
        fill = make_fill("f1", Side.BUY, 100.0, 1.0, fee=1.0)
        first = self.core.record_fill(fill)
        second = self.core.record_fill(fill)

        self.assertIs(first.outcome, FillOutcome.RECORDED)
        self.assertIs(second.outcome, FillOutcome.DUPLICATE_FILL_ID)
        self.assertEqual(self.core.fills.count, 1)
        self.assertEqual(self.core.position(SYMBOL).qty, 1.0)
        self.assertEqual(self.core.trading_fees, 1.0)
        self.assertEqual(self.core.balance, 999.0)

    def test_sc2_duplicate_trade_id_with_new_fill_id_is_not_counted(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=1.0, trade_id="trade-1"))
        duplicate = self.core.record_fill(make_fill("f2", Side.BUY, 100.0, 1.0, fee=1.0, trade_id="trade-1"))

        self.assertIs(duplicate.outcome, FillOutcome.DUPLICATE_TRADE_ID)
        self.assertEqual(self.core.fills.count, 1)
        self.assertEqual(self.core.position(SYMBOL).qty, 1.0)
        self.assertEqual(self.core.fills.duplicate_count, 1)

    def test_duplicate_application_reports_zero_effect(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 2.0, fee=0.5))
        duplicate = self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 2.0, fee=0.5))

        self.assertFalse(duplicate.accepted)
        self.assertEqual(duplicate.realized_delta, 0.0)
        self.assertEqual(duplicate.trading_fee_applied, 0.0)
        self.assertEqual(duplicate.closed_qty, 0.0)
        self.assertEqual(duplicate.opened_qty, 0.0)
        self.assertEqual(duplicate.position.qty, 2.0)

    def test_replaying_the_same_stream_twice_changes_nothing(self) -> None:
        fills = [
            make_fill("f1", Side.BUY, 100.0, 2.0, fee=0.2, trade_id="t1"),
            make_fill("f2", Side.SELL, 110.0, 1.0, fee=0.1, trade_id="t2", exchange_ts=BASE_TS + 1),
            make_fill("f3", Side.SELL, 120.0, 2.0, fee=0.1, trade_id="t3", exchange_ts=BASE_TS + 2),
        ]
        for fill in fills:
            self.core.record_fill(fill)

        state_after_first_pass = (
            self.core.position(SYMBOL),
            self.core.balance,
            self.core.realized_trade_pnl,
            self.core.trading_fees,
            self.core.fills.count,
        )

        for fill in fills:
            outcome = self.core.record_fill(fill)
            self.assertTrue(outcome.outcome.is_duplicate)

        self.assertEqual(
            (
                self.core.position(SYMBOL),
                self.core.balance,
                self.core.realized_trade_pnl,
                self.core.trading_fees,
                self.core.fills.count,
            ),
            state_after_first_pass,
        )
        self.assertEqual(self.core.fills.duplicate_count, len(fills))

    def test_duplicate_does_not_move_risk_snapshot(self) -> None:
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=1.0))
        self.core.update_mark_price(SYMBOL, 110.0, timestamp=BASE_TS + 1)
        before = build_risk_snapshot(self.core, symbol=SYMBOL, now_ms=BASE_TS + 2)

        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0, fee=1.0))
        after = build_risk_snapshot(self.core, symbol=SYMBOL, now_ms=BASE_TS + 2)

        self.assertEqual(before, after)

    def test_duplicate_does_not_change_gate_decision(self) -> None:
        gate = RiskGate(RiskLimits(max_position_qty=5.0))
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0))
        self.core.update_mark_price(SYMBOL, 100.0, timestamp=BASE_TS + 1)
        proposal = OrderProposal(symbol=SYMBOL, side=Side.BUY, quantity=0.5, price=100.0)

        before = gate.evaluate(proposal, build_risk_snapshot(self.core, symbol=SYMBOL, now_ms=BASE_TS + 2))
        self.core.record_fill(make_fill("f1", Side.BUY, 100.0, 1.0))
        after = gate.evaluate(proposal, build_risk_snapshot(self.core, symbol=SYMBOL, now_ms=BASE_TS + 2))

        self.assertIs(before.decision, RiskDecisionType.ALLOW)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
