"""Fill Ledger：append-only 与去重（SC-2 的单元层）。"""

from __future__ import annotations

import unittest

from market.events.types import Venue
from portfolio.fills import FillLedger, FillOutcome
from portfolio.types import Fill, InvalidFillError, Side
from tests.support import make_fill


class FillTypeTest(unittest.TestCase):
    def test_signed_quantity_and_notional(self) -> None:
        buy = make_fill("f1", Side.BUY, 100.0, 2.0)
        sell = make_fill("f2", Side.SELL, 100.0, 2.0)
        self.assertEqual(buy.signed_quantity, 2.0)
        self.assertEqual(sell.signed_quantity, -2.0)
        self.assertEqual(buy.notional, 200.0)

    def test_fill_is_immutable(self) -> None:
        fill = make_fill("f1", Side.BUY, 100.0, 1.0)
        with self.assertRaises(Exception):
            fill.quantity = 2.0  # type: ignore[misc]

    def test_invalid_fills_are_rejected(self) -> None:
        cases = (
            {"fill_id": ""},
            {"symbol": ""},
            {"trade_id": ""},
            {"quantity": 0.0},
            {"quantity": -1.0},
            {"price": 0.0},
            {"fee": -0.5},
            {"exchange_ts": -1},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(InvalidFillError):
                    Fill(
                        fill_id=str(overrides.get("fill_id", "f1")),
                        order_id="o1",
                        venue=Venue.BINANCE,
                        symbol=str(overrides.get("symbol", "BTCUSDT")),
                        side=Side.BUY,
                        price=float(overrides.get("price", 100.0)),
                        quantity=float(overrides.get("quantity", 1.0)),
                        fee=float(overrides.get("fee", 0.0)),
                        fee_asset="USDT",
                        trade_id=str(overrides.get("trade_id", "t1")),
                        exchange_ts=int(overrides.get("exchange_ts", 1_000)),
                        receive_ts=1_000,
                    )


class FillLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = FillLedger()

    def test_records_in_order(self) -> None:
        first = make_fill("f1", Side.BUY, 100.0, 1.0)
        second = make_fill("f2", Side.SELL, 101.0, 1.0, trade_id="t2")

        self.assertIs(self.ledger.record(first), FillOutcome.RECORDED)
        self.assertIs(self.ledger.record(second), FillOutcome.RECORDED)

        self.assertEqual(self.ledger.fills, (first, second))
        self.assertEqual(self.ledger.count, 2)
        self.assertEqual(len(self.ledger), 2)
        self.assertEqual(self.ledger.duplicate_count, 0)

    def test_duplicate_fill_id_is_ignored(self) -> None:
        fill = make_fill("f1", Side.BUY, 100.0, 1.0)
        self.ledger.record(fill)

        duplicate = make_fill("f1", Side.BUY, 999.0, 5.0, trade_id="t-other")
        self.assertIs(self.ledger.record(duplicate), FillOutcome.DUPLICATE_FILL_ID)

        self.assertEqual(self.ledger.count, 1)
        self.assertEqual(self.ledger.fills, (fill,))
        self.assertEqual(self.ledger.duplicate_count, 1)

    def test_duplicate_trade_id_is_ignored(self) -> None:
        self.ledger.record(make_fill("f1", Side.BUY, 100.0, 1.0, trade_id="trade-1"))

        duplicate = make_fill("f2", Side.BUY, 100.0, 1.0, trade_id="trade-1")
        self.assertIs(self.ledger.record(duplicate), FillOutcome.DUPLICATE_TRADE_ID)
        self.assertEqual(self.ledger.count, 1)

    def test_has_detects_both_keys(self) -> None:
        self.ledger.record(make_fill("f1", Side.BUY, 100.0, 1.0, trade_id="trade-1"))

        self.assertTrue(self.ledger.has(make_fill("f1", Side.BUY, 1.0, 1.0, trade_id="other")))
        self.assertTrue(self.ledger.has(make_fill("other", Side.BUY, 1.0, 1.0, trade_id="trade-1")))
        self.assertFalse(self.ledger.has(make_fill("f9", Side.BUY, 1.0, 1.0, trade_id="trade-9")))

    def test_dedup_is_scoped_per_symbol_and_venue(self) -> None:
        self.ledger.record(make_fill("f1", Side.BUY, 100.0, 1.0, trade_id="t1", symbol="BTCUSDT"))

        other_symbol = make_fill("f1", Side.BUY, 100.0, 1.0, trade_id="t1", symbol="ETHUSDT")
        self.assertIs(self.ledger.record(other_symbol), FillOutcome.RECORDED)
        self.assertEqual(self.ledger.count, 2)

    def test_for_symbol_filters(self) -> None:
        self.ledger.record(make_fill("f1", Side.BUY, 100.0, 1.0, symbol="BTCUSDT"))
        self.ledger.record(make_fill("f2", Side.BUY, 10.0, 1.0, symbol="ETHUSDT"))

        self.assertEqual([fill.symbol for fill in self.ledger.for_symbol("ETHUSDT")], ["ETHUSDT"])

    def test_outcome_flags(self) -> None:
        self.assertTrue(FillOutcome.RECORDED.accepted)
        self.assertFalse(FillOutcome.RECORDED.is_duplicate)
        self.assertTrue(FillOutcome.DUPLICATE_FILL_ID.is_duplicate)
        self.assertFalse(FillOutcome.DUPLICATE_TRADE_ID.accepted)


if __name__ == "__main__":
    unittest.main()
